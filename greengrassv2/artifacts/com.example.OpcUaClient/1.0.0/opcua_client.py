"""OPC UA client component for AWS IoT Greengrass V2.

Reads nodes from an OPC UA server and publishes readings to IoT Core.
Parameters are adjustable at runtime two ways:
  - live MQTT commands on <COMMAND_TOPIC>  (ephemeral)
  - component configuration updates        (persistent, deployment-driven)
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

ipc = None  # set in main()


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------

class Params:
    """Runtime parameters. Mutated from IPC callback threads, read from asyncio."""

    def __init__(self):
        self._lock = threading.Lock()
        self.endpoint = "opc.tcp://127.0.0.1:4840/freeopcua/server/"
        self.node_ids = []
        self.poll_interval = 5.0
        self.enabled = True

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

            if "enabled" in cfg:
                self.enabled = bool(cfg["enabled"])

        log.info("params now: %s", self.snapshot())

    def snapshot(self):
        with self._lock:
            return {
                "endpoint": self.endpoint,
                "nodeIds": list(self.node_ids),
                "pollIntervalSeconds": self.poll_interval,
                "enabled": self.enabled,
            }


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

async def read_loop():
    while True:
        p = params.snapshot()

        if not p["enabled"] or not p["nodeIds"]:
            await asyncio.sleep(min(p["pollIntervalSeconds"], 5.0))
            continue

        active_endpoint = p["endpoint"]

        try:
            async with Client(url=active_endpoint) as client:
                log.info("connected to %s", active_endpoint)

                while True:
                    p = params.snapshot()

                    # reconnect if the endpoint changed under us
                    if p["endpoint"] != active_endpoint:
                        log.info("endpoint changed, reconnecting")
                        break
                    if not p["enabled"]:
                        log.info("disabled, disconnecting")
                        break
                    if not p["nodeIds"]:
                        await asyncio.sleep(p["pollIntervalSeconds"])
                        continue

                    readings = {}
                    for nid in p["nodeIds"]:
                        try:
                            node = client.get_node(nid)
                            readings[nid] = jsonable(await node.read_value())
                        except Exception as exc:
                            readings[nid] = None
                            log.warning("read %s failed: %s", nid, exc)

                    await publish(TELEMETRY_TOPIC, {
                        "thing": THING_NAME,
                        "timestamp": time.time(),
                        "readings": readings,
                    })

                    await asyncio.sleep(p["pollIntervalSeconds"])

        except (ua.UaError, OSError, asyncio.TimeoutError) as exc:
            log.warning("opc ua connection problem: %s - retrying in 10s", exc)
            await asyncio.sleep(10)
        except Exception:
            log.exception("unexpected error - retrying in 10s")
            await asyncio.sleep(10)


def connect_ipc(attempts=10, delay=3):
    """The nucleus socket may not be ready the instant Run starts."""
    for i in range(1, attempts + 1):
        try:
            return GreengrassCoreIPCClientV2()
        except Exception as exc:
            log.warning("IPC connect attempt %d/%d failed: %s", i, attempts, exc)
            time.sleep(delay)
    raise RuntimeError("could not establish IPC connection to the nucleus")


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

    log.info("subscribed: commands=%s telemetry=%s", COMMAND_TOPIC, TELEMETRY_TOPIC)
    asyncio.run(read_loop())


if __name__ == "__main__":
    main()
