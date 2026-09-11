# Phase 1 operator runbook

This runbook operates the Phase 1 CodexBar-to-MQTT bridge on the Mac. It follows the
[observed CodexBar contract](observed-contract.md) and the
[Home Assistant entity map](entity-map.md).

## Current credential state

The CodexBar configuration is a credential store. Treat
`~/.config/codexbar/config.json`, Claude/Codex credential files, Keychain entries, MQTT
passwords, environment files, and launchd job output as credential-bearing material.
Never paste their contents into logs, issues, fixtures, or Home Assistant attributes.

## Runtime topology

Phase 1 has two long-lived local processes but only one process that touches provider
quota endpoints:

1. `codexbar serve` binds to loopback and refreshes upstream data every 300 seconds.
2. The bridge polls the bare `http://127.0.0.1:<port>/usage` endpoint, normalises healthy
   rows, and publishes retained MQTT discovery and state messages.

The bridge must not shell out to `codexbar usage`, add `?provider=...`, call
`/dashboard/v1/snapshot`, or use `--provider all`. Those paths create extra cache/upstream
lanes or attempt all 69 registered providers. Polling the cached local `/usage` endpoint
more frequently than 300 seconds does not change the upstream refresh interval.

Bind CodexBar to loopback. Do not use `--host 0.0.0.0 --allow-plain-http`. If a future
deployment fronts loopback with Tailscale Serve, remember that `/usage` and `/cost` are
not bearer-token protected; tailnet ACLs become the protection boundary, and networked
identity output must use `--identity redacted`.

## Required inputs

The Phase 1 configuration file is `~/.config/agent-telemetry/config.toml`. Keep this file
local and permission-restricted. `CODEXBAR_URL` may override the configured CodexBar URL;
its default is `http://127.0.0.1:8899/usage`.

| Input | Required value or constraint |
|---|---|
| CodexBar executable | Local CodexBar v0.55.1-compatible binary |
| CodexBar bind address | Loopback only (`127.0.0.1`) |
| CodexBar port | `8899`, unless both the Serve job and bridge URL are changed together |
| CodexBar refresh interval | Explicitly `300` seconds |
| Usage URL | `CODEXBAR_URL`, defaulting to bare `http://127.0.0.1:8899/usage`, with no provider query |
| MQTT host and port | Home Assistant broker endpoint; port 1883 is reachable and rejects anonymous access |
| MQTT username and password | Dedicated broker credentials obtained from the operator; do not reuse or commit unrelated credentials |
| MQTT client ID | Stable and unique for this collector |
| MQTT state topic prefix | `agent_telemetry` |
| Home Assistant discovery prefix | `homeassistant` |
| Local bridge poll interval | A reasonable interval for the cached local endpoint; it does not govern upstream refresh |
| Collector host identity | Stable Mac/host identifier used for device identity and future host attribution |

Store secrets in the permission-restricted TOML file, or in another local secret store
supported by the implementation. Do not put secrets in the plist command line: process
arguments and plist files are easy to inspect. Restrict credential and environment files
to the operator account.

## Preflight

Before loading launchd jobs:

1. Confirm CodexBar resolves the three enabled providers using a single bare usage read.
   Parse the JSON body and inspect provider-level errors; do not rely only on process exit
   status or HTTP status.
2. Confirm each enabled provider reports healthy. When checking CodexBar's configuration,
   inspect structure only; never print token values.
3. Confirm the configured local port is free and the serve URL is reachable only through
   loopback.
4. Obtain dedicated MQTT credentials. A successful TCP connection to port 1883 is not an
   authenticated publish test.
5. Complete the retained MQTT round-trip below before enabling Home Assistant discovery.

Claude may continue to report `dataConfidence: "percentOnly"` under its CLI source. That
is supported by the schema and does not block Phase 1. Do not change Claude to OAuth unless
its local credentials have first been verified to contain the required `user:profile`
scope; a missing scope requires operator reauthentication.

## Retained MQTT round-trip gate

Use the repository's integration check or an MQTT client that supports authentication.
The exact command depends on the implementation, but the test must prove all of these
properties:

1. Connect with the same broker host, port, username, and TLS settings intended for the
   service.
2. Publish a unique, harmless test payload to a bridge-owned test topic with the retain
   flag set.
3. Disconnect the publisher completely.
4. Start a fresh subscriber and verify it immediately receives the exact retained payload.
5. With the collector stopped, restart the broker through the supported Home
   Assistant/Mosquitto workflow and determine whether the broker persisted the retained
   payload. Record this result; it distinguishes broker persistence from collector repair.
6. Start the collector again. A fresh subscriber must receive the exact retained payload,
   either immediately from broker persistence or after the collector's full republish on
   reconnect.
7. Clear the test retained message by publishing an empty retained payload to that exact
   test topic.

Do not proceed to discovery/entity validation if authentication, authorisation, retention,
or broker persistence fails. Record only pass/fail evidence and sanitised topic names;
never record the MQTT password.

## launchd lifecycle

Install the shipped launchd property lists under `~/Library/LaunchAgents` as per-user
LaunchAgents. Keep CodexBar Serve
and the bridge as separately supervised jobs so each can be restarted and diagnosed
without creating a second provider poller. Both jobs should use absolute executable and
working-directory paths because launchd does not inherit an interactive shell's `PATH`.

The CodexBar job must set `HOME`, `XDG_CONFIG_HOME`, and a `PATH` containing
`/opt/homebrew/bin`, then run the equivalent of:

```text
codexbar serve --host 127.0.0.1 --port 8899 --refresh-interval 300
```

The bridge job must read the configured bare `/usage` URL and MQTT settings. Both jobs
use a restrictive `Umask` for log output. Configure launchd to start both jobs at login
and keep them alive after unexpected exits. Load
CodexBar first; the bridge must tolerate temporary connection failures and retry without
publishing fabricated states.

The launchd labels are `io.github.originallgb.codexbar-serve` and
`io.github.originallgb.agent-telemetry`. The shipped plists are templates: `__HOME__` and
`__REPO__` are replaced with your home directory and checkout path at install time.
`scripts/setup-mqtt.sh` does this for you; to install by hand, run from the checkout:

```sh
for label in io.github.originallgb.codexbar-serve io.github.originallgb.agent-telemetry; do
  sed -e "s#__HOME__#$HOME#g" -e "s#__REPO__#$PWD#g" "launchd/$label.plist" \
    > "$HOME/Library/LaunchAgents/$label.plist"
  chmod 600 "$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
done

launchctl print "gui/$(id -u)/io.github.originallgb.codexbar-serve"
launchctl print "gui/$(id -u)/io.github.originallgb.agent-telemetry"

launchctl bootout "gui/$(id -u)/io.github.originallgb.agent-telemetry"
launchctl bootout "gui/$(id -u)/io.github.originallgb.codexbar-serve"
```

The CodexBar job expects `codexbar` at `/opt/homebrew/bin/codexbar`; edit the template if
yours lives elsewhere.

`KeepAlive` starts each job after bootstrap. Use `launchctl kickstart -k` only for a
deliberate restart after changing a plist or configuration; it terminates the current
process first.

After loading, verify that only one `codexbar serve` process owns the configured port and
that no periodic `codexbar usage`, dashboard, or `--provider all` command is running.

## Operational verification

Validate the system in this order:

1. Fetch the bare loopback `/usage` endpoint twice inside the 300-second refresh window.
   Provider `updatedAt` values should remain unchanged, demonstrating a cached read.
2. Confirm a failure in one synthetic/fixture provider row does not prevent healthy rows
   from being published.
3. Confirm each current window produces a remaining-percent battery entity and reset
   timestamp, including all dynamic Antigravity extra windows.
4. Confirm pace attributes appear where supplied and are absent, not fabricated, for
   Antigravity.
5. Confirm `dataConfidence` is visible and Claude's observed `percentOnly` value is
   preserved.
6. Confirm the provider-level Codex reset-credit count is present when supplied.
7. Restart the bridge and verify a new subscriber/Home Assistant still sees retained
   state while the bridge is down and after it returns. Confirm the host-scoped private
   inventory manifest is restored and can tombstone an intentionally removed test entity.
8. Restart the broker and verify discovery plus state are restored, either from broker
   persistence or the collector's complete retained republish on reconnect. The bridge
   must also republish its private inventory manifest after a complete healthy snapshot.

The Phase 1 success criterion is a Home Assistant device with dynamic per-provider quota
and reset entities that survives both collector and broker restarts.

## Failure handling

- **One provider has an error:** publish all healthy provider rows. Keep the failed
  provider's last retained state; log a sanitised row-level error. Do not tombstone any
  topic or advance the retained inventory manifest from this partial snapshot.
- **`/usage` is temporarily unavailable or non-200:** attempt to parse a returned body;
  never gate healthy-row publication on HTTP status. A non-200 response is not eligible
  for deletion or inventory advancement. If no valid rows exist, publish no fabricated
  replacement states and retry.
- **CodexBar Serve exits:** let launchd restart it. The bridge retries the loopback URL and
  leaves retained MQTT states intact.
- **The bridge exits:** let launchd restart it. CodexBar Serve remains the only upstream
  lane.
- **MQTT is unavailable:** retry with backoff and leave broker-retained state untouched.
- **A desired publish, tombstone, or manifest publish fails:** keep the previous inventory
  authoritative and retry the idempotent reconciliation. Never record the new inventory
  before every desired publish and obsolete-topic tombstone is confirmed.
- **A historical retained topic predates the inventory manifest:** do not infer ownership
  from a broad broker scan. Remove it only through an explicit, host-scoped operator
  cleanup after verifying its discovery and state topics belong to this bridge.
- **A credential appears in output:** stop sharing the output, revoke/rotate the affected
  grant, remove exposed token material, and reauthenticate only through the provider's
  supported flow.

Phase 1 does not include hooks, enforcement, quota leases, cost fan-in, or Google Home.
Do not add a `hooks watch` process as part of this lifecycle.
