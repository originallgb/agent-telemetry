# Agent telemetry

Shows your AI coding-agent quotas in Home Assistant.

[CodexBar](https://github.com/steipete/CodexBar) already knows how much of your Codex,
Claude, Antigravity and other provider quotas you have used, when each window resets, and
whether you are on pace to run out. This bridge publishes that data to MQTT with Home
Assistant discovery, so every quota window appears as a sensor you can put on a dashboard
or use in an automation.

For each provider window it publishes:

- remaining percentage, with pace attributes (expected usage, whether it will last to reset, ETA)
- reset time as a timestamp sensor
- data confidence (`exact` or `percentOnly`)
- Codex reset credits, when CodexBar reports them

Entities are generated from whatever windows CodexBar returns, and removed again when a
window disappears. Everything is published retained, so readings survive both bridge and
broker restarts.

## How it works

```text
provider APIs ──► codexbar serve (loopback, refresh every 300 s)
                        │  GET /usage (cached, no upstream cost)
                        ▼
                  agent_telemetry.py ──► MQTT broker ──► Home Assistant
```

`codexbar serve` is the only process that contacts provider APIs, so the bridge can poll
it as often as it likes without increasing upstream traffic or risking rate limits. A
provider that errors is skipped for that cycle; the healthy ones are still published.

## Requirements

- macOS (the shipped service definitions are launchd LaunchAgents)
- [CodexBar](https://github.com/steipete/CodexBar) with its `codexbar` CLI, signed in to
  the providers you use (tested against v0.55.1)
- Home Assistant with the Mosquitto broker add-on and a dedicated MQTT user
- Python 3.14 and [uv](https://docs.astral.sh/uv/)

## Quick start

```sh
git clone https://github.com/originallgb/agent-telemetry.git
cd agent-telemetry
./scripts/setup-mqtt.sh
```

The wizard asks for your broker address and MQTT credentials. It proves a retained publish
round-trips, writes `~/.config/agent-telemetry/config.toml` (mode `0600`), installs and
starts both LaunchAgents, then checks the entities appear in Home Assistant.

To set it up by hand instead:

```sh
uv sync
mkdir -p ~/.config/agent-telemetry
install -m 600 config.example.toml ~/.config/agent-telemetry/config.toml
$EDITOR ~/.config/agent-telemetry/config.toml      # set mqtt.host, username, password
uv run python agent_telemetry.py --config ~/.config/agent-telemetry/config.toml --validate
uv run python agent_telemetry.py --config ~/.config/agent-telemetry/config.toml --once
```

`--validate` checks the config and CodexBar without touching MQTT; `--once` publishes a
single snapshot and exits. Then install the LaunchAgents as described in the
[operator runbook](docs/operator-runbook.md#launchd-lifecycle).

## Configuration

See [`config.example.toml`](config.example.toml). The MQTT password can live in the
config file (which must then be mode `0600`) or in `AGENT_TELEMETRY_MQTT_PASSWORD`.
`CODEXBAR_URL` overrides the CodexBar endpoint, which must be a loopback address.

## Documentation

- [Operator runbook](docs/operator-runbook.md): installation, lifecycle, verification, failure handling
- [Entity map](docs/entity-map.md): the Home Assistant entity contract
- [Observed CodexBar contract](docs/observed-contract.md): what CodexBar's payload actually contains

## Development

```sh
uv sync
uv run pytest
python3 scripts/validate-doc-links.py
plutil -lint launchd/*.plist
```

## License

[MIT](LICENSE)
