# greengrassv2
## Example using OpcUa Client on a Raspberry Pi

AWS IoT Greengrass V2 components for a Raspberry Pi edge device. Contents of
`~/greengrassv2/` the artifacts uploaded to S3 and the recipes registered
with AWS.

Device: `Test_1` · Region: `us-east-2` · Nucleus: `2.18.2`

## Layout

```
artifacts/                              files uploaded to S3
  com.example.OpcUaClient/
    1.0.0/opcua_client.py
    1.0.2/opcua_client.py
recipes/                                registered with create-component-version
  com.example.OpcUaClient-1.0.0.json
  com.example.OpcUaClient-1.0.2.json
testing/                                runs locally, never deployed
  test_server_debug.py
  read_once.py
  discover.py
CHEATSHEET.md
```

Every published version is kept because AWS component versions are immutable.
— Once registered, a version can never be changed, only superseded. The
directory layout mirrors the S3 key structure exactly:

```
s3://greengrass-artifacts-<account>/artifacts/<ComponentName>/<Version>/<file>
```

## com.example.OpcUaClient

Connects to an OPC UA server, reads configured nodes on an interval, and
publishes readings to IoT Core. Parameters are adjustable at runtime without
redeploying. Run opcua_server.py in another termial to test.

### Versions

| Version | Change |
|---|---|
| 1.0.0 | Initial. Polling loop, IPC publish, config subscription. |
| 1.0.2 | Event-driven wakeup so parameter changes apply mid-sleep. Per-read timeouts, exponential reconnect backoff, session drop after repeated total failure, status topic. |

### Configuration

| Key | Default | Meaning |
|---|---|---|
| `endpoint` | `opc.tcp://127.0.0.1:4840/freeopcua/server/` | OPC UA server URL |
| `nodeIds` | `["ns=2;s=Temperature", "ns=2;s=Pressure"]` | Nodes to read each cycle |
| `pollIntervalSeconds` | `5` | Seconds between reads |
| `readTimeoutSeconds` | `5` | Per-node read timeout |
| `enabled` | `true` | Master switch |

Configuration changes do not require a version bump. Code changes do.

### MQTT topics

| Topic | Direction | Purpose |
|---|---|---|
| `factory/Test_1/telemetry` | out | Readings, one message per poll |
| `factory/Test_1/status` | out | State transitions only |
| `factory/Test_1/command` | in | Live parameter changes, not persistent |

All three must appear in the recipe's `accessControl` block. IPC calls on a
topic not listed there fail with an authorization error.

### Telemetry payload

```json
{
  "thing": "Test_1",
  "timestamp": 1753718400.12,
  "endpoint": "opc.tcp://127.0.0.1:4840/freeopcua/server/",
  "readings": { "ns=2;s=Temperature": 21.4, "ns=2;s=Pressure": 101.1 },
  "failed": []
}
```

A node that fails to read appears as `null` in `readings` and is listed in
`failed`, which distinguishes a read error from a genuinely null value.

### Status states

`connected` · `ok` · `partial` · `degraded` · `disconnected` · `disabled` ·
`idle` · `error`

Published only on transition, so the topic stays quiet while healthy.

### Failure behaviour

- A single node failing does not stop the others, and telemetry still publishes
- A read that hangs is cut off by `readTimeoutSeconds` and counted as failed
- All nodes failing for 3 consecutive cycles drops and rebuilds the session
- An unreachable server retries with backoff from 5s up to 120s
- A crash is restarted by Greengrass; repeated crashes mark the component
  `BROKEN` and roll the deployment back
- No cloud connection means the nucleus spools QoS-1 messages in memory
  (~2.5 MB default, lost on restart)

## testing/

Local OPC UA tools, not part of any component or deployment.

- `test_server_debug.py` — server exposing Temperature and Pressure with
  protocol-level logging and graceful shutdown
- `read_once.py` — builds one `ReadRequest` by hand and prints the response,
  including a deliberately bad node so `BadNodeIdUnknown` is visible
- `discover.py` — dumps endpoints, security policies, identity tokens,
  namespaces, server profiles, and the address space

## Notes

`ns=2` is an index into the server's NamespaceArray, not a stable identifier.
String node IDs (`ns=2;s=Temperature`) survive address-space changes; numeric
auto-assigned ones (`ns=2;i=2`) renumber when nodes are added or removed.

Device certificates and private keys live in `/greengrass/v2/` on the device
and are deliberately not in this repo.
