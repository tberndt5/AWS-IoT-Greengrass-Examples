"""OPC UA client component for AWS IoT Greengrass V2.

Reads nodes from an OPC UA server and publishes readings to IoT Core.

Parameters are adjustable at runtime two ways:
  - live MQTT commands on <COMMAND_TOPIC>  (ephemeral)
  - component configuration updates        (persistent, deployment-driven)

Parameter changes take effect immediately, even mid-sleep, because the
IPC callback threads signal an asyncio.Event that the read loop waits on.
"""

import asyncio
import json
import logging
import os
import sys
import threading
import time

from asyncua import Client, ua
from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2
from awsiot.greengrasscoreipc.model import QOS

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("opcua-client")

THING_NAME = os.environ.get("AWS_IOT_THING_NAME", "Test_1")
TELEMETRY_TOPIC = f"factory/{THING_NAME}/telemetry"
COMMAND_TOPIC = f"factory/{THING_NAME}/command"
STATUS_TOPIC = f"factory/{THING_NAME}/status"

# reconnect backoff bounds, seconds
RECONNECT_BASE = 5.0
RECONNECT_MAX = 120.0

# consecutive cycles with every node failing before we drop the session
MAX_TOTAL_FAILURE_CYCLES = 3

ipc = None  # set in main()


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------

class Params:
    """Runtime parameters. Written from IPC callback threads, read from asyncio.

    After any change, wakes the read loop via loop.call_soon_threadsafe so a
    long poll interval never delays a parameter update.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._loop = None
        self._changed = None

        self.endpoint = "opc.tcp://127.0.0.1:4840/freeopcua/server/"
        self.node_ids = []
        self.poll_interval = 5.0
        self.read_timeout = 5.0
        self.enabled = True

    def bind(self, loop, changed):
        """Attach the running loop and its wakeup event."""
        with self._lock:
            self._loop = loop
            self._changed = changed

    def update(self, cfg):
        if not isinstance(cfg, dict):
            log.warning("ignoring non-dict config: %r", cfg)
            return

        with self._lock:
            if "endpoint" in cfg:
                self.endpoint = str(cfg["endpoint"])

            if "nodeIds" in cfg:
                try:
                    self.node_ids = [str(n) for n in cfg["nodeIds"]]
                except TypeError:
                    log.warning("nodeIds not iterable: %r", cfg["nodeIds"])

            if "pollIntervalSeconds" in cfg:
                try:
                    self.poll_interval = max(0.1, float(cfg["pollIntervalSeconds"]))
                except (TypeError, ValueError):
                    log.warning("bad pollIntervalSeconds: %r", cfg["pollIntervalSeconds"])

            if "readTimeoutSeconds" in cfg:
                try:
                    self.read_timeout = max(0.5, float(cfg["readTimeoutSeconds"]))
                except (TypeError, ValueError):
                    log.warning("bad readTimeoutSeconds: %r", cfg["readTimeoutSeconds"])

            if "enabled" in cfg:
                self.enabled = bool(cfg["enabled"])

        log.info("params now: %s", self.snapshot())
        self._wake()

    def snapshot(self):
        with self._lock:
            return {
                "endpoint": self.endpoint,
                "nodeIds": list(self.node_ids),
                "pollIntervalSeconds": self.poll_interval,
                "readTimeoutSeconds": self.read_timeout,
                "enabled": self.enabled,
            }

    def _wake(self):
        # must not hold the lock while touching the loop
        with self._lock:
            loop, changed = self._loop, self._changed
        if loop is not None and changed is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(changed.set)
            except RuntimeError:
                pass  # loop shutting down


params = Params()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def jsonable(value):
    """Coerce an OPC UA value into something json.dumps can handle."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return str(value)


async def publish(topic, payload):
    """Publish without blocking the event loop."""
    try:
        body = json.dumps(payload).encode()
    except (TypeError, ValueError):
        log.exception("payload not serializable, dropping")
        return

    try:
        await asyncio.to_thread(
            ipc.publish_to_iot_core,
            topic_name=topic,
            qos=QOS.AT_LEAST_ONCE,
            payload=body,
        )
    except Exception:
        log.exception("publish to %s failed", topic)


async def note_state(previous, state, detail=None):
    """Publish to the status topic only when the state actually changes."""
    if state == previous:
        return previous
    log.info("state: %s -> %s %s", previous, state, detail or "")
    await publish(STATUS_TOPIC, {
        "thing": THING_NAME,
        "timestamp": time.time(),
        "state": state,
        "detail": detail or {},
    })
    return state


async def sleep_or_wake(changed, timeout):
    """Sleep up to `timeout`, returning early if a parameter changed."""
    try:
        await asyncio.wait_for(changed.wait(), timeout=timeout)
        log.info("parameter change, waking early")
        return True
    except asyncio.TimeoutError:
        return False


# --------------------------------------------------------------------------
# IPC callbacks (run on SDK threads, never on the asyncio loop)
# --------------------------------------------------------------------------

def on_command(event):
    """Live MQTT parameter change."""
    try:
        raw = event.message.payload.decode()
        log.info("command received: %s", raw)
        params.update(json.loads(raw))
    except Exception:
        log.exception("could not apply command")


def on_config_update(event):
    """Deployment-driven configuration change."""
    try:
        resp = ipc.get_configuration(key_path=[])
        params.update(resp.value or {})
    except Exception:
        log.exception("could not fetch updated configuration")


def on_stream_error(error):
    log.error("IPC stream error: %s", error)
    return False  # keep the stream open


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

async def poll_once(client, p):
    """Read every configured node. Returns (readings, failed_node_ids)."""
    readings, failed = {}, []

    for nid in p["nodeIds"]:
        try:
            node = client.get_node(nid)
            value = await asyncio.wait_for(
                node.read_value(), timeout=p["readTimeoutSeconds"]
            )
            readings[nid] = jsonable(value)
        except asyncio.TimeoutError:
            readings[nid] = None
            failed.append(nid)
            log.warning("read %s timed out after %.1fs", nid, p["readTimeoutSeconds"])
        except Exception as exc:
            readings[nid] = None
            failed.append(nid)
            log.warning("read %s failed: %s", nid, exc)

    return readings, failed


async def read_loop(changed):
    backoff = RECONNECT_BASE
    state = None

    while True:
        changed.clear()
        p = params.snapshot()

        if not p["enabled"]:
            state = await note_state(state, "disabled")
            await sleep_or_wake(changed, 60.0)
            continue

        if not p["nodeIds"]:
            state = await note_state(state, "idle", {"reason": "no nodeIds configured"})
            await sleep_or_wake(changed, 60.0)
            continue

        endpoint = p["endpoint"]

        try:
            async with Client(url=endpoint, timeout=p["readTimeoutSeconds"]) as client:
                log.info("connected to %s", endpoint)
                backoff = RECONNECT_BASE
                state = await note_state(state, "connected", {"endpoint": endpoint})
                total_failure_cycles = 0

                while True:
                    changed.clear()
                    p = params.snapshot()

                    if p["endpoint"] != endpoint:
                        log.info("endpoint changed to %s, reconnecting", p["endpoint"])
                        break
                    if not p["enabled"]:
                        break
                    if not p["nodeIds"]:
                        await sleep_or_wake(changed, p["pollIntervalSeconds"])
                        continue

                    readings, failed = await poll_once(client, p)

                    await publish(TELEMETRY_TOPIC, {
                        "thing": THING_NAME,
                        "timestamp": time.time(),
                        "endpoint": endpoint,
                        "readings": readings,
                        "failed": failed,
                    })

                    if failed and len(failed) == len(p["nodeIds"]):
                        total_failure_cycles += 1
                        state = await note_state(
                            state, "degraded",
                            {"failed": failed, "cycles": total_failure_cycles},
                        )
                        if total_failure_cycles >= MAX_TOTAL_FAILURE_CYCLES:
                            log.error(
                                "every node failed %d cycles, dropping session",
                                total_failure_cycles,
                            )
                            break
                    else:
                        total_failure_cycles = 0
                        if failed:
                            state = await note_state(state, "partial", {"failed": failed})
                        else:
                            state = await note_state(state, "ok")

                    await sleep_or_wake(changed, p["pollIntervalSeconds"])

        except (ua.UaError, OSError, asyncio.TimeoutError) as exc:
            state = await note_state(
                state, "disconnected",
                {"endpoint": endpoint, "error": str(exc), "retryInSeconds": backoff},
            )
            log.warning("connection problem: %s - retrying in %.0fs", exc, backoff)
            await sleep_or_wake(changed, backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

        except Exception:
            log.exception("unexpected error - retrying in %.0fs", backoff)
            state = await note_state(state, "error", {"retryInSeconds": backoff})
            await sleep_or_wake(changed, backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)


# --------------------------------------------------------------------------
# startup
# --------------------------------------------------------------------------

def connect_ipc(attempts=10, delay=3):
    """The nucleus socket may not be ready the instant Run starts."""
    for i in range(1, attempts + 1):
        try:
            return GreengrassCoreIPCClientV2()
        except Exception as exc:
            log.warning("IPC connect attempt %d/%d failed: %s", i, attempts, exc)
            time.sleep(delay)
    raise RuntimeError("could not establish IPC connection to the nucleus")


async def run():
    changed = asyncio.Event()
    params.bind(asyncio.get_running_loop(), changed)
    await read_loop(changed)


def main():
    global ipc

    log.info("starting; thing=%s", THING_NAME)
    ipc = connect_ipc()

    try:
        resp = ipc.get_configuration(key_path=[])
        params.update(resp.value or {})
    except Exception:
        log.exception("could not read initial configuration, using defaults")

    ipc.subscribe_to_iot_core(
        topic_name=COMMAND_TOPIC,
        qos=QOS.AT_LEAST_ONCE,
        on_stream_event=on_command,
        on_stream_error=on_stream_error,
    )
    ipc.subscribe_to_configuration_update(
        key_path=[],
        on_stream_event=on_config_update,
        on_stream_error=on_stream_error,
    )

    log.info(
        "subscribed: commands=%s telemetry=%s status=%s",
        COMMAND_TOPIC, TELEMETRY_TOPIC, STATUS_TOPIC,
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
