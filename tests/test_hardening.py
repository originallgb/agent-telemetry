import json
import time
from copy import deepcopy

import pytest

from conftest import FakeBroker, FakeMQTTClient


def _write_config(path, *, url="http://127.0.0.1:8899/usage", password=None):
    password_line = f'password = "{password}"\n' if password is not None else ""
    path.write_text(
        "[codexbar]\n"
        f'url = "{url}"\n'
        "[mqtt]\n"
        'host = "mqtt.internal"\n'
        f"{password_line}"
    )
    return path


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://192.0.2.10:8899/usage",
        "http://localhost:8899/usage-not-bare",
        "http://127.0.0.1:8899/usage/extra",
        "http://127.0.0.1:8899/usage?provider=all",
        "http://127.0.0.1:8899/usage#fragment",
        "https://127.0.0.1:8899/usage",
    ],
)
def test_load_settings_rejects_non_loopback_or_non_bare_usage_url(
    tmp_path, monkeypatch, bad_url
):
    from agent_telemetry import load_settings

    config = _write_config(tmp_path / "config.toml")
    config.chmod(0o600)
    monkeypatch.setenv("CODEXBAR_URL", bad_url)

    with pytest.raises(ValueError, match="loopback|127\\.0\\.0\\.1|/usage"):
        load_settings(config)


def test_load_settings_rejects_password_in_world_readable_config(tmp_path, monkeypatch):
    from agent_telemetry import load_settings

    monkeypatch.delenv("CODEXBAR_URL", raising=False)
    config = _write_config(tmp_path / "config.toml", password="do-not-log-this")
    config.chmod(0o644)

    with pytest.raises(PermissionError, match="chmod 600"):
        load_settings(config)


def test_untrusted_pace_cannot_override_attributes_or_add_secret(fake_broker):
    from agent_telemetry import publish_snapshot

    client = FakeMQTTClient(fake_broker)
    snapshot = {
        "rows": [
            {
                "provider": "codex",
                "window_id": "primary",
                "title": "5-hour",
                "remaining_percent": 75,
                "resets_at": "2026-08-28T01:00:00Z",
                "window_minutes": 300,
                "data_confidence": "exact",
                "source": "oauth",
                "updated_at": "2026-08-27T23:00:00Z",
                "pace": {
                    "stage": "onTrack",
                    "deltaPercent": 2,
                    "provider": "attacker-controlled",
                    "window_id": "attacker-window",
                    "source": "attacker-source",
                    "resets_at": "1999-01-01T00:00:00Z",
                    "secret": "must-not-be-published",
                },
            }
        ],
        "reset_credits": [],
        "errors": [],
    }

    publish_snapshot(client, snapshot, host_id="test-host")

    attributes_call = next(
        call for call in client.calls if call["topic"].endswith("/attributes")
    )
    attributes = json.loads(attributes_call["payload"])
    assert attributes["provider"] == "codex"
    assert attributes["window_id"] == "primary"
    assert attributes["source"] == "oauth"
    assert attributes["resets_at"] == "2026-08-28T01:00:00Z"
    assert attributes["stage"] == "onTrack"
    assert attributes["deltaPercent"] == 2
    assert "secret" not in attributes


def test_on_connect_is_nonblocking_and_main_thread_republishes(normalized):
    from agent_telemetry import Bridge, Settings

    class CallbackGuardClient:
        def publish(self, *args, **kwargs):
            raise AssertionError("on_connect must not publish or wait in MQTT callback")

    settings = Settings(
        codexbar_url="http://127.0.0.1:8899/usage",
        poll_interval=30,
        request_timeout=20,
        mqtt_host="mqtt.internal",
        mqtt_port=1883,
        mqtt_username=None,
        mqtt_password=None,
        mqtt_client_id="hardening-test",
        topic_prefix="agent_telemetry",
        discovery_prefix="homeassistant",
        host_id="test-host",
    )
    bridge = Bridge(CallbackGuardClient(), settings)
    bridge.cache_snapshot(normalized, complete=True)

    started = time.monotonic()
    bridge.on_connect(bridge.client, None, None, 0)
    elapsed = time.monotonic() - started

    assert elapsed < 0.1
    assert bridge.connected.is_set()

    broker = FakeBroker()
    bridge.client = FakeMQTTClient(broker)
    assert bridge.republish_if_needed() is True
    assert broker.retained
    assert bridge.republish_if_needed() is False


def test_topic_encoding_is_collision_resistant_and_has_no_mqtt_wildcards():
    from agent_telemetry import publish_snapshot

    broker = FakeBroker()
    client = FakeMQTTClient(broker)
    base_row = {
        "title": "Synthetic",
        "remaining_percent": 50,
        "resets_at": None,
        "window_minutes": 300,
        "data_confidence": "exact",
        "source": "test",
        "updated_at": "2026-08-27T23:00:00Z",
        "pace": None,
    }
    snapshot = {
        "rows": [
            {**deepcopy(base_row), "provider": "a/b", "window_id": "five+hour"},
            {**deepcopy(base_row), "provider": "a_b", "window_id": "five#hour"},
        ],
        "reset_credits": [],
        "errors": [],
    }

    publish_snapshot(client, snapshot, host_id="test/host")

    topics = [call["topic"] for call in client.calls]
    assert all("+" not in topic and "#" not in topic for topic in topics)
    state_topics = {
        topic for topic in topics if topic.endswith("/remaining/state")
    }
    assert len(state_topics) == 2, "a/b and a_b must not collapse to one MQTT topic"
