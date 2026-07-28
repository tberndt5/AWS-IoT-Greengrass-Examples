"""Test OPC UA server with protocol-level logging and graceful shutdown.

Run this in one terminal, then run read_once.py in another to watch a
single Read service call cross the wire.

    python3 -m venv ~/opcua-test
    ~/opcua-test/bin/pip install asyncua
    ~/opcua-test/bin/python test_server_debug.py

Ctrl-C (SIGINT) or `kill <pid>` (SIGTERM) shuts down cleanly: the server
closes open secure channels and sessions instead of dropping the socket.
"""

import asyncio
import logging
import random
import signal

from asyncua import Server, ua

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-42s %(levelname)-5s %(message)s",
)

# The interesting loggers. Comment these out for a quiet server.
#
#   binary_server_asyncio - raw TCP framing: HEL/ACK/OPN/MSG/CLO chunks
#   uaprocessor           - one line per service request and response
#   connection            - secure channel open/renew/close
#
logging.getLogger("asyncua.server.binary_server_asyncio").setLevel(logging.DEBUG)
logging.getLogger("asyncua.server.uaprocessor").setLevel(logging.DEBUG)
logging.getLogger("asyncua.common.connection").setLevel(logging.DEBUG)

# Uncomment for absolutely everything, including address-space lookups.
# logging.getLogger("asyncua").setLevel(logging.DEBUG)

log = logging.getLogger("test-server")

UPDATE_INTERVAL = 1.0


def install_signal_handlers(stop_event):
    """Ask the loop to set `stop_event` on SIGINT / SIGTERM.

    add_signal_handler runs the callback inside the event loop, so the
    shutdown happens between awaits rather than interrupting one.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows; fall back to the KeyboardInterrupt path in __main__
            pass


async def main():
    stop = asyncio.Event()
    install_signal_handlers(stop)

    server = Server()
    await server.init()
    server.set_endpoint("opc.tcp://0.0.0.0:4840/freeopcua/server/")
    server.set_server_name("Learning OPC UA Server")

    # SecurityPolicy None keeps the traffic plaintext so Wireshark can
    # dissect it. Never do this on a real network.
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    idx = await server.register_namespace("http://example.org")
    machine = await server.nodes.objects.add_object(idx, "Machine")

    temperature = await machine.add_variable(
        ua.NodeId("Temperature", idx), "Temperature", 20.0
    )
    pressure = await machine.add_variable(
        ua.NodeId("Pressure", idx), "Pressure", 101.3
    )

    await temperature.set_writable()
    await pressure.set_writable()

    log.info("namespace index: %s", idx)
    log.info("temperature node: %s", temperature.nodeid.to_string())
    log.info("pressure node:    %s", pressure.nodeid.to_string())
    log.info("listening on opc.tcp://0.0.0.0:4840/freeopcua/server/")
    log.info("press Ctrl-C to stop")

    updates = 0

    async with server:
        while not stop.is_set():
            await temperature.write_value(20.0 + random.uniform(-2, 2))
            await pressure.write_value(101.3 + random.uniform(-0.5, 0.5))
            updates += 1

            # Sleep, but wake immediately if a shutdown signal arrives.
            try:
                await asyncio.wait_for(stop.wait(), timeout=UPDATE_INTERVAL)
            except asyncio.TimeoutError:
                pass

        log.info("shutdown requested after %d updates, closing server", updates)

    # `async with server:` has now called server.stop(), which closes
    # sessions and secure channels and releases port 4840.
    log.info("server stopped cleanly")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Only reached if add_signal_handler was unavailable.
        log.info("interrupted")
