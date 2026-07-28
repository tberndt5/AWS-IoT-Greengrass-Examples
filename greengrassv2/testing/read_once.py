"""Perform exactly one OPC UA Read and print the request and response.

Run test_server_debug.py in another terminal first.

    ~/opcua-test/bin/python read_once.py

Unlike node.read_value(), this builds the ReadParameters structure by hand
so you can see every field the Read service actually carries.
"""

import asyncio
import logging

from asyncua import Client, ua

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-40s %(levelname)-5s %(message)s",
)

# Client-side view of the same exchange.
logging.getLogger("asyncua.client.ua_client").setLevel(logging.DEBUG)
logging.getLogger("asyncua.common.connection").setLevel(logging.DEBUG)

log = logging.getLogger("read-once")

ENDPOINT = "opc.tcp://127.0.0.1:4840/freeopcua/server/"
NODES = ["ns=2;s=Temperature", "ns=2;s=Pressure", "ns=2;i=2"]  # last one is bogus


def describe(dv, node_id):
    """Unpack a DataValue the way the spec defines it."""
    status = dv.StatusCode
    print(f"\n  node          {node_id}")
    print(f"  StatusCode    {status.value:#010x}  {status.name}")
    print(f"  is_good       {status.is_good()}")
    if dv.Value is not None:
        print(f"  Variant type  {dv.Value.VariantType.name}")
        print(f"  Value         {dv.Value.Value!r}")
    print(f"  SourceTime    {dv.SourceTimestamp}")
    print(f"  ServerTime    {dv.ServerTimestamp}")


async def main():
    async with Client(url=ENDPOINT) as client:
        print("\n" + "=" * 70)
        print("session established; building ReadRequest")
        print("=" * 70)

        params = ua.ReadParameters()

        # MaxAge=0 means "do not serve me a cached value, go to the device"
        params.MaxAge = 0

        # Which timestamps the server should attach to each DataValue
        params.TimestampsToReturn = ua.TimestampsToReturn.Both

        # One ReadValueId per node/attribute pair
        for node_id in NODES:
            rv = ua.ReadValueId()
            rv.NodeId = ua.NodeId.from_string(node_id)

            # 13 = Value. Try NodeClass, BrowseName, DisplayName, DataType
            # to read metadata instead of the reading itself.
            rv.AttributeId = ua.AttributeIds.Value

            # IndexRange is an optional String, so None is legal here.
            # It selects a slice when the node holds an array, e.g. "0:3".
            rv.IndexRange = None

            # DataEncoding is a QualifiedName *struct*, not an optional
            # string. It must be an empty QualifiedName, never None: the
            # binary encoder calls dataclasses.fields() on whatever is
            # here, and NoneType is not a dataclass. An empty name means
            # "use the default (binary) encoding".
            rv.DataEncoding = ua.QualifiedName()

            params.NodesToRead.append(rv)

        print("\n--- ReadRequest parameters ---")
        print(f"MaxAge              {params.MaxAge}")
        print(f"TimestampsToReturn  {params.TimestampsToReturn.name}")
        for rv in params.NodesToRead:
            print(f"NodesToRead[]       NodeId={rv.NodeId.to_string():24s} "
                  f"AttributeId={rv.AttributeId.name}({rv.AttributeId.value})")

        # This is the actual service call. The RequestHeader (auth token,
        # request handle, timeout hint) is filled in by the stack.
        results = await client.uaclient.read(params)

        print("\n--- ReadResponse results ---")
        for node_id, dv in zip(NODES, results):
            describe(dv, node_id)

        print("\n" + "=" * 70)
        print("closing session")
        print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
