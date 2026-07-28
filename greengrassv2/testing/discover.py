"""Dump every URI and capability an OPC UA server advertises.

Run test_server_debug.py in another terminal first.

    ~/opcua-test/bin/python discover.py

Three separate things get printed:

  1. Endpoints      - from GetEndpoints, which needs no session at all
  2. Namespaces     - the NamespaceArray, which gives meaning to "ns=2"
  3. Profiles       - ServerProfileArray: URIs naming which service sets
                      the server actually implements
"""

import asyncio
import logging

from asyncua import Client, ua

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

# Uncomment to see the protocol traffic behind all of this.
# logging.getLogger("asyncua").setLevel(logging.DEBUG)

ENDPOINT = "opc.tcp://127.0.0.1:4840/freeopcua/server/"

RULE = "=" * 72


def header(text):
    print(f"\n{RULE}\n{text}\n{RULE}")


async def show_endpoints(client):
    """GetEndpoints runs over a bare SecureChannel, before any session."""
    header("1. ENDPOINTS  (GetEndpoints service)")

    endpoints = await client.connect_and_get_server_endpoints()

    for i, ep in enumerate(endpoints, 1):
        print(f"\n--- endpoint {i} of {len(endpoints)} ---")
        print(f"  EndpointUrl          {ep.EndpointUrl}")
        print(f"  SecurityMode         {ep.SecurityMode.name}")
        print(f"  SecurityPolicyUri    {ep.SecurityPolicyUri}")
        print(f"  SecurityLevel        {ep.SecurityLevel}")
        print(f"  TransportProfileUri  {ep.TransportProfileUri}")

        app = ep.Server
        print(f"  ApplicationUri       {app.ApplicationUri}")
        print(f"  ProductUri           {app.ProductUri}")
        print(f"  ApplicationName      {app.ApplicationName.Text}")
        print(f"  ApplicationType      {app.ApplicationType.name}")
        for url in app.DiscoveryUrls or []:
            print(f"  DiscoveryUrl         {url}")

        print("  accepted identity tokens:")
        for tok in ep.UserIdentityTokens or []:
            print(f"    - PolicyId={tok.PolicyId!r} "
                  f"TokenType={tok.TokenType.name} "
                  f"SecurityPolicyUri={tok.SecurityPolicyUri}")

        if ep.ServerCertificate:
            print(f"  ServerCertificate    {len(ep.ServerCertificate)} bytes")
        else:
            print("  ServerCertificate    (none - security is disabled)")


async def show_namespaces(client):
    """ns=2 is only meaningful relative to this array."""
    header("2. NAMESPACES  (NamespaceArray, node i=2255)")

    namespaces = await client.get_namespace_array()
    for idx, uri in enumerate(namespaces):
        print(f"  ns={idx}  {uri}")

    print("\n  so 'ns=2;s=Temperature' means the node named 'Temperature'")
    print(f"  in the namespace {namespaces[2] if len(namespaces) > 2 else '(none)'}")


async def show_profiles(client):
    """ServerProfileArray is the closest thing to 'which services exist'."""
    header("3. SUPPORTED PROFILES  (Server/ServerCapabilities/ServerProfileArray)")

    try:
        node = await client.nodes.server.get_child(
            ["0:ServerCapabilities", "0:ServerProfileArray"]
        )
        profiles = await node.read_value()
        for uri in profiles or []:
            print(f"  {uri}")
        if not profiles:
            print("  (empty - this server does not declare its profiles)")
    except Exception as exc:
        print(f"  could not read ServerProfileArray: {exc}")


async def show_server_status(client):
    header("4. SERVER STATUS  (Server/ServerStatus)")

    try:
        node = await client.nodes.server.get_child(["0:ServerStatus"])
        status = await node.read_value()
        build = status.BuildInfo
        print(f"  State                {status.State.name}")
        print(f"  StartTime            {status.StartTime}")
        print(f"  CurrentTime          {status.CurrentTime}")
        print(f"  ManufacturerName     {build.ManufacturerName}")
        print(f"  ProductName          {build.ProductName}")
        print(f"  ProductUri           {build.ProductUri}")
        print(f"  SoftwareVersion      {build.SoftwareVersion}")
        print(f"  BuildNumber          {build.BuildNumber}")
    except Exception as exc:
        print(f"  could not read ServerStatus: {exc}")


async def browse(node, depth=0, max_depth=3, seen=None):
    """Walk the address space. Every node you can read lives somewhere here."""
    if seen is None:
        seen = set()

    nid = node.nodeid.to_string()
    if nid in seen or depth > max_depth:
        return
    seen.add(nid)

    try:
        name = await node.read_browse_name()
        cls = await node.read_node_class()
    except Exception:
        return

    print(f"  {'  ' * depth}{name.to_string():30s} {nid:24s} {cls.name}")

    try:
        for child in await node.get_children():
            await browse(child, depth + 1, max_depth, seen)
    except Exception:
        pass


async def show_address_space(client):
    header("5. ADDRESS SPACE  (Browse service, from Objects down)")
    print(f"  {'BrowseName':30s} {'NodeId':24s} NodeClass")
    print("  " + "-" * 66)
    await browse(client.nodes.objects)


async def main():
    client = Client(url=ENDPOINT)

    # Step 1 needs no session, so it runs against a fresh client.
    await show_endpoints(client)
    await client.disconnect()

    # Everything after this needs an activated session.
    async with Client(url=ENDPOINT) as connected:
        await show_namespaces(connected)
        await show_profiles(connected)
        await show_server_status(connected)
        await show_address_space(connected)

    print()


if __name__ == "__main__":
    asyncio.run(main())
