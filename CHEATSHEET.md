# Cheat Sheet
## AWS IoT Greengrass setup and cheat sheet for CLI

Verified on a Raspberry Pi (aarch64), Greengrass nucleus 2.18.2.

**Set these once per shell.** Every command below uses them. Replace the
account ID, thing name, and bucket with your own.

```bash
export AWS_REGION=us-east-2
export ACCOUNT=471028395610
export THING=Test_1
export BUCKET=greengrass-artifacts-$ACCOUNT
export NUCLEUS=2.18.2
```
**The account number is a random number...nice try**
---

# Part 1 — From scratch

## 1. Check the architecture

```bash
uname -m
```

`aarch64` is what you want. `armv7l` means 32-bit — no AWS CLI v2 exists for it.

## 2. Install packages

```bash
sudo apt update
sudo apt install -y default-jdk python3-venv python3-dev unzip curl git
java -version
```

## 3. Install AWS CLI v2

```bash
curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o awscliv2.zip
unzip awscliv2.zip
sudo ./aws/install
aws --version
```

## 4. Configure credentials

```bash
aws configure
aws sts get-caller-identity
```

Must show `user/...`, not `:root`.

## 5. Download the nucleus

```bash
curl -s https://d2s8p88vqu9w66.cloudfront.net/releases/greengrass-nucleus-latest.zip \
  > greengrass-nucleus-latest.zip
unzip greengrass-nucleus-latest.zip -d GreengrassInstaller
```

## 6. Provision the device

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=...

sudo -E env \
  AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  AWS_REGION="$AWS_REGION" \
  java -Droot="/greengrass/v2" -Dlog.store=FILE \
  -jar ./GreengrassInstaller/lib/Greengrass.jar \
  --aws-region "$AWS_REGION" \
  --thing-name "$THING" \
  --component-default-user ggc_user:ggc_group \
  --provision true \
  --setup-system-service true \
  --deploy-dev-tools true
```

Notes:
- Ignore `Unable to set up Nucleus as a system service` — step 8 handles it.
- These keys are needed once. After this the device uses its own certificate.

## 7. Create the launch directory

The installer often doesn't. Without it, every deployment fails with
`LAUNCH_DIRECTORY_CORRUPTED`.

```bash
sudo mkdir -p /greengrass/v2/alts/init

sudo ln -sfn \
  /greengrass/v2/packages/artifacts-unarchived/aws.greengrass.Nucleus/$NUCLEUS/aws.greengrass.nucleus \
  /greengrass/v2/alts/init/distro

sudo rm -rf /greengrass/v2/alts/current
sudo ln -sfn /greengrass/v2/alts/init /greengrass/v2/alts/current
```

Check it resolves all the way through:

```bash
sudo namei -l /greengrass/v2/alts/current/distro/bin/loader
```

## 8. Create the service

Edit the nucleus version in `ExecStart` if yours differs, systemd does not
expand shell variables.

```bash
sudo tee /etc/systemd/system/greengrass.service > /dev/null <<'EOF'
[Unit]
Description=Greengrass Core
After=network.target

[Service]
Type=simple
Restart=on-failure
RestartSec=30
ExecStart=/usr/bin/java -Dlog.store=FILE -Droot=/greengrass/v2 -jar /greengrass/v2/packages/artifacts-unarchived/aws.greengrass.Nucleus/2.18.2/aws.greengrass.nucleus/lib/Greengrass.jar --setup-system-service false

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now greengrass.service
```

## 9. Verify

```bash
sudo systemctl status greengrass.service
```

Want `Active: active (running)` with a PID that stays the same when you check
again a minute later.

```bash
aws greengrassv2 get-core-device --core-device-thing-name $THING --region $AWS_REGION
```

Want `HEALTHY` with a recent timestamp.

## 10. Create the artifact bucket

```bash
aws s3 mb s3://$BUCKET --region $AWS_REGION
```

Then in the console: **IAM → Roles → GreengrassV2TokenExchangeRole → Add
permissions → Create inline policy**, name it `GreengrassS3ArtifactAccess`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject"],
    "Resource": "arn:aws:s3:::greengrass-artifacts-471028395610/*"
  }]
}
```
**The number following greengrass-artifacts-# is your Account number. This one is fake obviously**

The trailing `/*` matters. Without this, deployments fail to download artifacts.

---

# Part 2 — Create a new component

Building `com.example.SensorLogger` from nothing and getting it running.

## 1. Name it and make the folders

Reverse-DNS naming. Pick it carefully — renaming later means republishing.

```bash
export COMPONENT=com.example.SensorLogger
export VERSION=1.0.0
export SHORT=sensorlogger

mkdir -p artifacts/$COMPONENT/$VERSION
mkdir -p recipes
```

## 2. Write the code

```bash
cat > artifacts/$COMPONENT/$VERSION/main.py <<'EOF'
import json
import logging
import os
import sys
import time

from awsiot.greengrasscoreipc.clientv2 import GreengrassCoreIPCClientV2
from awsiot.greengrasscoreipc.model import QOS

logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sensorlogger")

THING_NAME = os.environ.get("AWS_IOT_THING_NAME", "unknown")
TELEMETRY_TOPIC = f"factory/{THING_NAME}/sensorlogger/telemetry"
COMMAND_TOPIC = f"factory/{THING_NAME}/sensorlogger/command"

interval = 10.0
ipc = None


def connect_ipc(attempts=10, delay=3):
    """The nucleus socket may not be ready the instant Run starts."""
    for i in range(1, attempts + 1):
        try:
            return GreengrassCoreIPCClientV2()
        except Exception as exc:
            log.warning("IPC attempt %d/%d failed: %s", i, attempts, exc)
            time.sleep(delay)
    raise RuntimeError("no IPC connection to the nucleus")


def on_command(event):
    global interval
    try:
        cfg = json.loads(event.message.payload.decode())
        if "intervalSeconds" in cfg:
            interval = max(1.0, float(cfg["intervalSeconds"]))
            log.info("interval now %.1fs", interval)
    except Exception:
        log.exception("bad command")


def main():
    global ipc, interval

    log.info("starting; thing=%s", THING_NAME)
    ipc = connect_ipc()

    config = ipc.get_configuration(key_path=[]).value or {}
    interval = float(config.get("intervalSeconds", 10))
    log.info("config: %s", config)

    ipc.subscribe_to_iot_core(
        topic_name=COMMAND_TOPIC,
        qos=QOS.AT_LEAST_ONCE,
        on_stream_event=on_command,
    )
    log.info("publishing to %s every %.1fs", TELEMETRY_TOPIC, interval)

    while True:
        payload = {"thing": THING_NAME, "timestamp": time.time(), "uptime": time.monotonic()}
        ipc.publish_to_iot_core(
            topic_name=TELEMETRY_TOPIC,
            qos=QOS.AT_LEAST_ONCE,
            payload=json.dumps(payload).encode(),
        )
        time.sleep(interval)


if __name__ == "__main__":
    main()
EOF
```

A long-running component must never return from `main()`. One that exits shows
as `FINISHED`, not `RUNNING`.

## 3. Write the recipe

```bash
cat > recipes/$COMPONENT-$VERSION.json <<EOF
{
  "RecipeFormatVersion": "2020-01-25",
  "ComponentName": "$COMPONENT",
  "ComponentVersion": "$VERSION",
  "ComponentDescription": "Publishes a heartbeat to IoT Core.",
  "ComponentPublisher": "Me",
  "ComponentDependencies": {
    "aws.greengrass.Nucleus": {
      "VersionRequirement": ">=2.0.0 <3.0.0",
      "DependencyType": "SOFT"
    }
  },
  "ComponentConfiguration": {
    "DefaultConfiguration": {
      "intervalSeconds": 10,
      "accessControl": {
        "aws.greengrass.ipc.mqttproxy": {
          "$COMPONENT:mqttproxy:1": {
            "policyDescription": "Publish telemetry and receive commands.",
            "operations": [
              "aws.greengrass#PublishToIoTCore",
              "aws.greengrass#SubscribeToIoTCore"
            ],
            "resources": [
              "factory/$THING/$SHORT/telemetry",
              "factory/$THING/$SHORT/command"
            ]
          }
        }
      }
    }
  },
  "Manifests": [
    {
      "Platform": { "os": "linux" },
      "Lifecycle": {
        "Install": {
          "Timeout": 900,
          "Script": "python3 -m venv {work:path}/venv && {work:path}/venv/bin/pip install --upgrade pip && {work:path}/venv/bin/pip install awsiotsdk"
        },
        "Run": "{work:path}/venv/bin/python -u {artifacts:path}/main.py"
      },
      "Artifacts": [
        {
          "Uri": "s3://$BUCKET/artifacts/$COMPONENT/$VERSION/main.py"
        }
      ]
    }
  ]
}
EOF
```

Three things that must line up or the component fails silently:

- topics in `accessControl` must match the topics in the code, exactly
- `Run` must point at the filename you actually uploaded
- `Timeout: 900` — `awscrt` may compile from source on ARM

## 4. Validate

```bash
python3 -m py_compile artifacts/$COMPONENT/$VERSION/main.py
python3 -m json.tool recipes/$COMPONENT-$VERSION.json > /dev/null
```

Silence means both passed.

## 5. Upload and register

```bash
aws s3 cp artifacts/$COMPONENT/$VERSION/main.py \
  s3://$BUCKET/artifacts/$COMPONENT/$VERSION/main.py

aws greengrassv2 create-component-version \
  --inline-recipe fileb://recipes/$COMPONENT-$VERSION.json \
  --region $AWS_REGION
```

## 6. Deploy it alongside what's already there

**A deployment document is the complete desired state for its target.** Any
component you leave out gets removed from the device. To add the new one and
keep the existing ones, list them all.

Check what's on the device now:

```bash
aws greengrassv2 list-installed-components \
  --core-device-thing-name $THING --region $AWS_REGION \
  --query 'installedComponents[*].{Name:componentName,Version:componentVersion}' \
  --output table
```

Then include everything you want running:

```bash
cat > deploy.json <<'EOF'
{
  "com.example.OpcUaClient":  { "componentVersion": "1.0.2" },
  "com.example.SensorLogger": { "componentVersion": "1.0.0" }
}
EOF

aws greengrassv2 create-deployment \
  --target-arn "arn:aws:iot:$AWS_REGION:$ACCOUNT:thing/$THING" \
  --components file://deploy.json \
  --region $AWS_REGION
```

Ignore `aws.greengrass.Cli` and `aws.greengrass.Nucleus` in that list — they
came from a different deployment target and are managed separately.

## 7. Confirm it's running

```bash
sudo tail -f /greengrass/v2/logs/com.example.SensorLogger.log
```

First run takes a few minutes while the venv builds. The log file only appears
once `Run` starts, so `Install` failures show up in `greengrass.log` instead.

Watch the data arrive: **IoT Core → MQTT test client → subscribe to
`factory/Test_1/sensorlogger/telemetry`**

## 8. Change its interval without redeploying

```bash
aws iot-data publish \
  --topic "factory/$THING/sensorlogger/command" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"intervalSeconds": 30}' \
  --region $AWS_REGION
```

---

# Part 3 — Update an existing component

Going from 1.0.2 to 1.0.3.

## 1. Copy the code forward and edit

```bash
mkdir -p artifacts/com.example.OpcUaClient/1.0.3
cp artifacts/com.example.OpcUaClient/1.0.2/opcua_client.py \
   artifacts/com.example.OpcUaClient/1.0.3/opcua_client.py
nano artifacts/com.example.OpcUaClient/1.0.3/opcua_client.py
```

## 2. Bump the recipe — two places

```bash
cp recipes/com.example.OpcUaClient-1.0.2.json \
   recipes/com.example.OpcUaClient-1.0.3.json
sed -i 's|1\.0\.2|1.0.3|g' recipes/com.example.OpcUaClient-1.0.3.json
grep -n "1\.0\.3" recipes/com.example.OpcUaClient-1.0.3.json
```

Should print exactly two lines: `ComponentVersion` and the artifact `Uri`.

## 3. Validate

```bash
python3 -m py_compile artifacts/com.example.OpcUaClient/1.0.3/opcua_client.py
python3 -m json.tool recipes/com.example.OpcUaClient-1.0.3.json > /dev/null
```

## 4. Upload, register, deploy

```bash
aws s3 cp artifacts/com.example.OpcUaClient/1.0.3/opcua_client.py \
  s3://$BUCKET/artifacts/com.example.OpcUaClient/1.0.3/opcua_client.py

aws greengrassv2 create-component-version \
  --inline-recipe fileb://recipes/com.example.OpcUaClient-1.0.3.json \
  --region $AWS_REGION

cat > deploy.json <<'EOF'
{
  "com.example.OpcUaClient":  { "componentVersion": "1.0.3" },
  "com.example.SensorLogger": { "componentVersion": "1.0.0" }
}
EOF

aws greengrassv2 create-deployment \
  --target-arn "arn:aws:iot:$AWS_REGION:$ACCOUNT:thing/$THING" \
  --components file://deploy.json \
  --region $AWS_REGION
```

## 5. Watch

```bash
sudo tail -f /greengrass/v2/logs/com.example.OpcUaClient.log
```

**Versions are immutable.** Overwriting the S3 file for an existing version
does nothing — the device already has it cached. Always bump.

---

# Part 4 — Change settings, no redeploy

## Live (instant, lost on restart)

```bash
aws iot-data publish \
  --topic "factory/$THING/command" \
  --cli-binary-format raw-in-base64-out \
  --payload '{"pollIntervalSeconds": 300}' \
  --region $AWS_REGION
```

## Persistent

```bash
cat > deploy.json <<'EOF'
{
  "com.example.OpcUaClient": {
    "componentVersion": "1.0.2",
    "configurationUpdate": {
      "merge": "{\"pollIntervalSeconds\": 300}"
    }
  },
  "com.example.SensorLogger": { "componentVersion": "1.0.0" }
}
EOF

aws greengrassv2 create-deployment \
  --target-arn "arn:aws:iot:$AWS_REGION:$ACCOUNT:thing/$THING" \
  --components file://deploy.json \
  --region $AWS_REGION
```

No version bump — config isn't code. `merge` replaces arrays wholesale, so send
the whole `nodeIds` list. Undo with `"reset": [""]`.

---

# Part 5 — Test server

```bash
python3 -m venv ~/opcua-test
~/opcua-test/bin/pip install asyncua

~/opcua-test/bin/python testing/test_server_debug.py     # terminal 1
~/opcua-test/bin/python testing/read_once.py             # terminal 2
~/opcua-test/bin/python testing/discover.py
```

Start the server before the component connects, or it just logs refusals.

---

# Part 6 — Checks

```bash
# is it running, and from the right path?
ps aux | grep -i "[g]reengrass"

# service state
sudo systemctl status greengrass.service

# nucleus log
sudo tail -f /greengrass/v2/logs/greengrass.log

# a component log
sudo tail -f /greengrass/v2/logs/com.example.OpcUaClient.log

# what logs exist at all
ls -la /greengrass/v2/logs/

# what's installed on the device
aws greengrassv2 list-installed-components \
  --core-device-thing-name $THING --region $AWS_REGION --output table

# device connected to AWS?
aws greengrassv2 get-core-device --core-device-thing-name $THING --region $AWS_REGION

# deployment results
aws greengrassv2 list-effective-deployments \
  --core-device-thing-name $THING --region $AWS_REGION \
  --query 'effectiveDeployments[*].{Status:coreDeviceExecutionStatus,Reason:reason}'

# search the nucleus log
sudo grep -i -E "deployment|denied|403" \
  /greengrass/v2/logs/greengrass.log | tail -40
```

Watch data in AWS: **IoT Core → MQTT test client → subscribe to
`factory/Test_1/#`**

---

# Part 7 — When it breaks

| What you see | What it means |
|---|---|
| `Unable to load credentials` | `sudo -E` isn't enough; pass keys via `env` |
| Component log file missing | The component has never run |
| `/greengrass/v2/bin/` empty | Nucleus isn't running, or CLI never deployed |
| `LAUNCH_DIRECTORY_CORRUPTED` | Redo Part 1 step 7 |
| `cannot open .../loader` | Same thing, seen from systemd |
| Nucleus running from an odd path | A second install with a different `-Droot` |
| Clean shutdown then nothing | Foreground process died with the terminal |
| `Exec format error` | Wrong CPU architecture download |
| `Please attach it to the IAM role` | Informational; prints forever, ignore |
| Component runs but no data in AWS | Topic missing from recipe `accessControl` |
| Code change did nothing | Version not bumped |
| A component vanished | It was left out of the last deployment document |
| `BadNodeIdUnknown` | Node ID doesn't exist on that server |
| 403 / AccessDenied on artifacts | S3 policy missing from the token exchange role |
