# Troubleshooting

Notes from setting this up the first time. Almost none of these were hard
problems once I understood them, but several cost me an hour because the error
message pointed somewhere other than the actual cause. Writing them down so
future me does not repeat them.

---

## I used root access keys

`aws sts get-caller-identity` came back with:

```
"Arn": "arn:aws:iam::123456789012:root"
```

That is the account root user. Root keys cannot be scoped by policy, cannot be
restricted, and cannot be revoked without disrupting the account owner. If they
leak, whoever has them owns everything including billing.

I deleted them and made an IAM user instead. Worth doing at the very start, not
after provisioning is already done.

---

## sudo -E did not pass my credentials

The installer died with a wall of text about the credential provider chain. The
useful line was buried in the middle:

```
ProfileCredentialsProvider(...): Profile file contained no credentials
for profile 'default': ProfileFile(sections=[])
```

Empty profile file. It was looking in `/root/.aws/credentials`, not mine.
Even with `-E`, sudo resets `HOME` on Debian, so the SDK resolved the profile
path against root's home directory.

The fix is to pass the keys through `env` explicitly instead of trusting `-E`:

```bash
sudo -E env \
  AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
  AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" \
  AWS_REGION="$AWS_REGION" \
  java -Droot="/greengrass/v2" ...
```

Related: I nearly exported an empty `AWS_SESSION_TOKEN` because the docs mention
one. Session tokens only exist for temporary credentials. My keys started with
`AKIA`, which means long term, so there was no token. An empty token is worse
than an absent one, because the SDK sends the header anyway and the request gets
rejected for a completely different reason.

---

## I passed --init-config and --provision at the same time

My first installer command had both:

```
--init-config /greengrass/v2/config.yaml
--provision true
```

Those contradict each other. `--init-config` means "here is my finished
configuration including certificate paths." `--provision true` means "go create
all of that for me." On top of that, the config file I pointed at did not exist
yet.

Dropped `--init-config` and it was fine.

---

## I installed Greengrass twice, in two different places

This one wasted the most time. My first attempt used:

```
-Droot="/home/tyler/greengrass/greengrass/v2"
```

Note the doubled `greengrass/greengrass`, which was a typo. Every later attempt
used `/greengrass/v2`. So I ended up with two complete installs.

Then `ps aux` showed this:

```
java ... -Droot=/home/tyler/greengrass/greengrass/v2 -jar .../Greengrass.jar
```

The install that was *running* had no valid device configuration. The install
that had my certificate and my pending deployments had never been started. That
explains why `/greengrass/v2/bin/` stayed empty, why the CLI never showed up, and
why component log files did not exist. I was reading the logs of one install
while a different one spun uselessly.

Two lessons. First, always check the `-Droot` value in `ps aux`, not just whether
something named greengrass is running. Second, a home directory is a bad root
anyway, because `/home/tyler` is mode 0700 and `ggc_user` cannot traverse into it
to run component code.

---

## An old systemd unit was fighting me in the background

`journalctl` showed this:

```
greengrass.service: Scheduled restart job, restart counter is at 621.
/bin/sh: 0: cannot open /greengrass/v2/alts/current/distro/bin/loader: No such file
```

Restart 621. It had been failing every ten seconds in the background for hours,
left over from an early install attempt, quietly stealing the root directory out
from under every manual start I attempted.

```bash
sudo systemctl stop greengrass.service
sudo systemctl disable greengrass.service
```

If something behaves as though another process is interfering, it probably is.
Check `systemctl list-units --all | grep greengrass` early.

---

## I kept killing the nucleus by closing my terminal

I ran the nucleus in the foreground to watch it start, then hit Ctrl-C or closed
the SSH session. The log looked like this:

```
KernelLifecycle: system-shutdown
KernelLifecycle: context-shutdown-complete
```

No exception, no crash, no error. Just an orderly shutdown, which is exactly what
a SIGTERM looks like. I spent a while assuming something was wrong with the
configuration when the process had simply been asked to stop.

Foreground is fine for watching startup. For anything longer, use systemd, or at
minimum `nohup ... &`.

---

## alts/current never got created

Deployments kept failing with:

```
"reason": "FAILED_NO_STATE_CHANGE: ... Greengrass launch directory is not
set up or Greengrass is not set up as a system service"
"errorStack": ["DEPLOYMENT_FAILURE", "LAUNCH_DIRECTORY_CORRUPTED"]
```

Greengrass expects a launch directory at `alts/current/distro`, which it uses to
restart itself during bootstrap and nucleus upgrades. It gets created on the
first fully successful startup. Mine never completed one, because of everything
above, so it never existed. And because it did not exist, deployments were
refused, which kept it from ever being created. A nice little deadlock.

I built it by hand:

```bash
sudo mkdir -p /greengrass/v2/alts/init
sudo ln -sfn /greengrass/v2/packages/artifacts-unarchived/aws.greengrass.Nucleus/2.18.2/aws.greengrass.nucleus \
  /greengrass/v2/alts/init/distro
sudo rm -rf /greengrass/v2/alts/current
sudo ln -sfn /greengrass/v2/alts/init /greengrass/v2/alts/current
```

Re-running the installer with `--start false` did not help, since it wants to
point the service at a loader that only exists once the nucleus has started.

---

## ln put the symlink inside the directory instead of replacing it

Classic. `alts/current` already existed as a real empty directory, so:

```bash
sudo ln -sfn /greengrass/v2/alts/init /greengrass/v2/alts/current
```

created `/greengrass/v2/alts/current/init` rather than replacing `current`. Then
`rmdir` refused to clean up:

```
rmdir: failed to remove '/greengrass/v2/alts/current': Directory not empty
```

because the stray link I had just made was sitting in it. `ln -sfn` will not
overwrite a real directory, and the error scrolled past inside a longer command.

`ls -la` on the parent is what gave it away. `current` showed as `d`, and `init`
showed as `l` with an arrow. One is a directory, one is a symlink, and only one
of those is correct.

`namei -l` is the right tool for this. It walks every component of a path and
prints where each link resolves:

```bash
sudo namei -l /greengrass/v2/alts/current/distro/bin/loader
```

---

## The same path gave two different errors depending on user

```
$ ls -la /greengrass/v2/alts/current/distro/bin/loader
Permission denied

$ sudo ls -la /greengrass/v2/alts/current/distro/bin/loader
No such file or directory
```

Both are correct. As my own user, a directory partway down the path is mode 0700,
so traversal was refused before the kernel ever reached the missing file. Root
traverses freely and gets to the real gap.

So "Permission denied" on a deep path does not necessarily mean permissions are
the problem. Check as root before concluding anything.

---

## I downloaded the wrong AWS CLI build

```
$ aws --version
-bash: /usr/local/bin/aws: cannot execute binary file: Exec format error
```

I grabbed `awscli-exe-linux-x86_64.zip` on an aarch64 Pi. The installer happily
put it in `/usr/local` and nothing complained until I tried to run it.

Check first:

```bash
uname -m
```

Then use `awscli-exe-linux-aarch64.zip`. Also worth checking `file aws/dist/aws`
before running `sudo ./aws/install`, since a wrong or truncated download is
cheaper to catch before it lands in `/usr/local`.

Do not use `apt install awscli`. Ubuntu and Raspberry Pi OS ship v1, which has no
`greengrassv2` subcommands at all.

---

## I read a log file that did not have the answer in it

I searched for MQTT activity and got almost nothing back, which made me think the
device had never connected:

```bash
sudo grep -i -E "mqtt|connect" /greengrass/v2/logs/greengrass.log
```

`greengrass.log` is only the current file. Older content rotates into timestamped
files. The window I was searching happened to contain only the shutdown sequence.

```bash
sudo grep -h -i "connect" /greengrass/v2/logs/greengrass*.log | tail -40
```

Also, I assumed a missing component log file meant something was broken. It just
means the component has never run. Component logs are created on first execution,
so their absence is a symptom, not the disease.

---

## I edited the deployed artifact and lost the change

To test a one line fix quickly, I edited this directly:

```
/greengrass/v2/packages/artifacts/com.example.OpcUaClient/1.0.0/opcua_client.py
```

That works for a fast check, and the component restarts with the edit. But the
next deployment re-extracted the artifact from cache and my change vanished. The
same error came right back in the log, which was confusing for a minute.

Deployed artifacts are a cache, not a source tree. Edit the real file and
republish.

---

## I forgot that component versions are immutable

Related to the above. Uploading a fixed script to the same S3 key does nothing,
because the device already has that version cached and will never re-download
it. Registering the same version again is rejected.

Any code change needs a version bump in two places: `ComponentVersion` and the
artifact `Uri` in the recipe. Configuration changes do not, which took me a
moment to internalize as a separate rule.

---

## Stale node IDs after I changed the server

I switched the test server from auto-assigned numeric node IDs to string ones,
then wondered why the component logged this every five seconds:

```
read ns=2;i=2 failed: The node id refers to a node that does not exist
in the server address space.(BadNodeIdUnknown)
```

The component was still configured with the old ID. Fixed with a live MQTT
command, then made it stick with a configuration merge.

The wider lesson is that auto-assigned numeric IDs like `ns=2;i=2` depend on
registration order. Insert a variable earlier in the server and everything after
it renumbers, silently. String IDs like `ns=2;s=Temperature` are stable, so I use
those now.

Also worth knowing: a bad node does not fail the whole Read request. The service
result comes back Good and the bad node carries its own status code. That is why
the component reports failures per node rather than throwing.

---

## Re-running provisioning made a second certificate

Every time I re-ran the installer with `--provision true`, it created a new key
pair and certificate and attached it to the same thing. After a few attempts:

```bash
aws iot list-thing-principals --thing-name Test_1 --region us-east-2
```

returned more ARNs than expected. The device uses the newest one and the rest sit
there active and unused.

To find out which is live, hash the certificate the device is actually using. The
certificate ID in IoT is the SHA-256 of the DER encoding, which is also the ARN
suffix:

```bash
sudo openssl x509 -in /greengrass/v2/thingCert.crt -outform DER | sha256sum
```

Deactivate a spare before deleting it, and confirm the device still connects.

The real lesson is to stop re-running the installer hoping a downstream problem
will fix itself. Provisioning succeeded on my first attempt. Everything after
that was noise.

---

## I trusted a stale deployment status

`list-effective-deployments` showed FAILED, and I started debugging. Then I
looked at `modifiedTimestamp` and realised it was from forty minutes earlier,
while the nucleus log showed the same deployment being received and resolved
minutes ago.

Always check the timestamp before reacting to a status.

---

## Things I would do differently

Get the nucleus running and confirmed before anything else. Three separate
problems I chased (empty `bin/`, missing component logs, deployments never
arriving) all traced back to the nucleus not running, or running from the wrong
root. Once it was up under systemd, everything downstream worked on the first
try.

Install the Greengrass CLI early and use local deployments while iterating.
Reading artifacts off disk turns a five minute S3 round trip into about five
seconds, and failures show up immediately instead of as a silent deployment
status.

Validate before uploading. Two commands that print nothing on success:

```bash
python3 -m py_compile artifacts/.../main.py
python3 -m json.tool recipes/....json > /dev/null
```

A syntax error caught here costs two seconds. The same error caught after a
deploy costs five minutes and a version bump.
