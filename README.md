# greengrassv2
## AWS IoT Greengrass V2 components for a Raspberry Pi edge device. 

Contents of
`~/greengrassv2/` — the artifacts uploaded to S3 and the recipes registered
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

---

## Prerequisites on the Pi

64-bit Raspberry Pi OS. Confirm before anything else, because the wrong
architecture is silent until a binary refuses to run:

```bash
uname -m        # aarch64
lsb_release -a
```

`armv7l` means a 32-bit OS. AWS ships no CLI v2 build for it — reflash to
64-bit, or run all AWS commands from another machine.

### Packages

```bash
sudo apt update
sudo apt install -y default-jdk python3-venv python3-dev unzip curl git

java -version         # 11 or newer
python3 --version     # 3.9 or newer
```

The Greengrass nucleus is a Java application even though the components here
are Python. `python3-venv` and `python3-dev` are needed by the component's
`Install` script — without them the venv creation or the `awscrt` build fails
with an unhelpful error.

### AWS CLI v2

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o awscliv2.zip
unzip awscliv2.zip
sudo ./aws/install
aws --version
```

Use `awscli-exe-linux-x86_64.zip` on x86. Do not use `apt install awscli` —
that installs v1, which has no `greengrassv2` subcommands.

### Greengrass nucleus installer

```bash
curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-nucleus-latest.zip \
  > greengrass-nucleus-latest.zip
unzip greengrass-nucleus-latest.zip -d GreengrassInstaller
```

Provisioning command is in `CHEATSHEET.md`. It needs AWS credentials once;
afterwards the device authenticates with its own X.509 certificate.

### Optional

```bash
sudo apt install -y tshark      # wire-level OPC UA capture
```

### Versions this was built against

| Component | Version |
|---|---|
| Greengrass nucleus | 2.18.2 |
| Java | OpenJDK 17 |
| Python | 3.13 |
| asyncua | 2.0.1 |
| awsiotsdk | 1.31.0 |
| awscrt | 0.36.1 |

---

## Test environment

A local OPC UA server so the component has something to read without touching
real equipment. Runs as your own user, entirely outside Greengrass.

### Setup

```bash
python3 -m venv ~/opcua-test
~/opcua-test/bin/pip install --upgrade pip
~/opcua-test/bin/pip install asyncua
```

Separate from the component's venv, which Greengrass builds under
`/greengrass/v2/work/<component>/venv` and owns.

### Running

Terminal 1 — the server:

```bash
~/opcua-test/bin/python testing/test_server_debug.py
```

It prints the node IDs it registered and then updates them once a second:

```
temperature node: ns=2;s=Temperature
pressure node:    ns=2;s=Pressure
listening on opc.tcp://0.0.0.0:4840/freeopcua/server/
```

Ctrl-C shuts it down cleanly, closing sessions and releasing port 4840.

Terminal 2 — one annotated Read:

```bash
~/opcua-test/bin/python testing/read_once.py
```

Builds a `ReadRequest` by hand and prints the response. Includes a deliberately
bad node ID so you can see `BadNodeIdUnknown` returned as a per-node
`StatusCode` while the request itself succeeds.

Everything the server advertises:

```bash
~/opcua-test/bin/python testing/discover.py
```

Endpoints, security policies, accepted identity tokens, the NamespaceArray,
supported service profiles, and the address space.

### Pointing the component at it

The component's default `endpoint` is already `opc.tcp://127.0.0.1:4840/freeopcua/server/`,
so with the test server running it connects on its own. The component runs as
`ggc_user` and the server as your user, since they talk over a TCP
socket on loopback.

Confirm data is flowing:

```bash
sudo tail -f /greengrass/v2/logs/com.example.OpcUaClient.log
```

Then subscribe to `factory/Test_1/#` in the IoT Core MQTT test client to see
telemetry and status arriving in AWS.

Start the test server **before** the component connects, or its first act is
logging connection refusals and backing off.

### Wire-level capture

```bash
sudo tshark -i lo -f "tcp port 4840" -Y opcua -V
```
---

## com.example.OpcUaClient

Connects to an OPC UA server, reads configured nodes on an interval, and
publishes readings to IoT Core. Parameters are adjustable at runtime without
redeploying.

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

---

## testing/

Local OPC UA tools, not part of any component or deployment.

- `test_server_debug.py` — server exposing Temperature and Pressure with
  protocol-level logging and graceful shutdown
- `read_once.py` — builds one `ReadRequest` by hand and prints the response,
  including a deliberately bad node so `BadNodeIdUnknown` is visible
- `discover.py` — dumps endpoints, security policies, identity tokens,
  namespaces, server profiles, and the address space

---

## Notes

`ns=2` is an index into the server's NamespaceArray, not a stable identifier.
String node IDs (`ns=2;s=Temperature`) survive address-space changes; numeric
auto-assigned ones (`ns=2;i=2`) renumber when nodes are added or removed.

Device certificates and private keys live in `/greengrass/v2/` on the device
and are deliberately not in this repo.
