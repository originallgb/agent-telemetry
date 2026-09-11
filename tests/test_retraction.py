"""Lifecycle coverage for bridge-owned retained MQTT topics."""

import json
from copy import deepcopy

import pytest
from conftest import FakeBroker, FakeMessageInfo, FakeMQTTClient


def _settings(**overrides):
    from agent_telemetry import Settings

    values = {
        "codexbar_url": "http://127.0.0.1:8899/usage",
        "poll_interval": 30,
        "request_timeout": 20,
        "mqtt_host": "mqtt.internal",
        "mqtt_port": 1883,
        "mqtt_username": None,
        "mqtt_password": None,
        "mqtt_client_id": "reconciliation-test",
        "topic_prefix": "agent_telemetry",
        "discovery_prefix": "homeassistant",
        "host_id": "test-host",
    }
    values.update(overrides)
    return Settings(**values)


def _row(provider, window_id, *, resets_at="2026-08-28T20:00:00Z"):
    return {
        "provider": provider,
        "window_id": window_id,
        "title": window_id.title(),
        "remaining_percent": 50.0,
        "resets_at": resets_at,
        "window_minutes": 300,
        "data_confidence": "exact",
        "source": "cli",
        "updated_at": "2026-08-28T16:00:00Z",
        "pace": None,
    }


def _credit(provider):
    return {
        "provider": provider,
        "available_count": 1,
        "updated_at": "2026-08-28T16:00:00Z",
    }


def _snapshot(rows, *, credits=None, errors=None):
    return {
        "rows": rows,
        "reset_credits": credits or [],
        "errors": errors or [],
    }


def _bridge(broker, settings=None, *, connect=False):
    from agent_telemetry import Bridge

    client = FakeMQTTClient(broker)
    bridge = Bridge(client, settings or _settings())
    client.on_message = bridge.on_message
    if connect:
        bridge.on_connect(client, None, None, 0)
    return bridge, client


def _tombstones(calls):
    return [call for call in calls if call["payload"] == ""]


def test_render_desired_topics_is_pure_complete_and_contract_compatible():
    from agent_telemetry import render_desired_topics

    snapshot = _snapshot([_row("codex", "primary")], credits=[_credit("codex")])
    original = deepcopy(snapshot)

    first = render_desired_topics(snapshot, host_id="test-host")
    second = render_desired_topics(snapshot, host_id="test-host")

    assert first == second
    assert snapshot == original
    assert len(first) == 8
    assert (
        first["agent_telemetry/test_host_c80cb283/codex/primary/remaining/state"]
        == "50.0"
    )
    config = json.loads(
        first[
            "homeassistant/sensor/agent_telemetry_test_host_c80cb283_codex_primary_remaining/config"
        ]
    )
    assert config["state_topic"].endswith("/codex/primary/remaining/state")
    assert any(topic.endswith("/reset/state") for topic in first)
    assert any("reset_credits" in topic for topic in first)


@pytest.mark.parametrize("removal", ["window", "reset", "credit"])
def test_complete_snapshot_tombstones_window_reset_and_credit_removals(removal):
    from agent_telemetry import manifest_topic

    broker = FakeBroker()
    bridge, client = _bridge(broker)
    initial = _snapshot(
        [_row("codex", "primary"), _row("codex", "weekly")],
        credits=[_credit("codex")],
    )
    bridge.publish_and_reconcile(initial, complete=True)

    if removal == "window":
        later = _snapshot([_row("codex", "weekly")], credits=[_credit("codex")])
        marker = "/primary/"
    elif removal == "reset":
        later = _snapshot(
            [_row("codex", "primary", resets_at=None), _row("codex", "weekly")],
            credits=[_credit("codex")],
        )
        marker = "codex_primary_reset"
    else:
        later = _snapshot([_row("codex", "primary"), _row("codex", "weekly")])
        marker = "reset_credits"

    before = len(client.calls)
    stale = bridge.publish_and_reconcile(later, complete=True)
    calls = client.calls[before:]
    removed = _tombstones(calls)

    assert stale
    assert removed
    assert all(call["qos"] == 1 and call["retain"] is True for call in removed)
    assert any(marker in call["topic"] for call in removed)
    assert all(topic not in broker.retained for topic in stale)
    assert calls[-1]["topic"] == manifest_topic("agent_telemetry", "test-host")


def test_complete_provider_removal_is_scoped_to_exact_manifest_inventory():
    broker = FakeBroker()
    bridge, _ = _bridge(broker)
    initial = _snapshot([_row("codex", "primary"), _row("claude", "primary")])
    bridge.publish_and_reconcile(initial, complete=True)
    foreign = {
        "agent_telemetry/other_host/claude/primary/remaining/state": "42",
        "homeassistant/sensor/some_other_integration/config": "{}",
    }
    broker.retained.update(foreign)

    stale = bridge.publish_and_reconcile(
        _snapshot([_row("codex", "primary")]),
        complete=True,
    )

    assert stale
    assert all("claude" in topic for topic in stale)
    assert not any(
        "/claude/" in topic for topic in broker.retained if topic not in foreign
    )
    assert all(topic in broker.retained for topic in foreign)
    assert any("/codex/" in topic for topic in broker.retained)


def test_partial_provider_failure_never_tombstones_or_advances_manifest():
    from agent_telemetry import manifest_topic

    broker = FakeBroker()
    bridge, client = _bridge(broker)
    initial = _snapshot([_row("codex", "primary"), _row("claude", "primary")])
    bridge.publish_and_reconcile(initial, complete=True)
    inventory_before = set(bridge.inventory)
    manifest = manifest_topic("agent_telemetry", "test-host")
    manifest_before = broker.retained[manifest]

    before = len(client.calls)
    bridge.publish_and_reconcile(
        _snapshot(
            [_row("codex", "primary")],
            errors=[{"provider": "claude", "kind": "provider"}],
        ),
        complete=False,
    )

    calls = client.calls[before:]
    assert not _tombstones(calls)
    assert broker.retained[manifest] == manifest_before
    assert bridge.inventory == inventory_before
    assert any("/claude/" in topic for topic in broker.retained)


def test_partial_snapshot_preserves_last_known_topics_for_broker_republish():
    from agent_telemetry import manifest_topic

    broker = FakeBroker()
    bridge, _ = _bridge(broker)
    initial = _snapshot([_row("codex", "primary"), _row("claude", "primary")])
    bridge.publish(initial, complete=True)
    inventory_before = set(bridge.inventory)

    refreshed_codex = _row("codex", "primary")
    refreshed_codex["remaining_percent"] = 25.0
    bridge.publish(
        _snapshot(
            [refreshed_codex],
            errors=[{"provider": "claude", "kind": "provider"}],
        ),
        complete=False,
    )

    broker.restart()
    bridge.on_connect(bridge.client, None, None, 0)
    assert bridge.republish_if_needed(manifest_timeout=0) is True

    assert any("/claude/" in topic for topic in broker.retained)
    manifest = manifest_topic("agent_telemetry", "test-host")
    assert json.loads(broker.retained[manifest])["topics"] == sorted(inventory_before)
    codex_state = next(
        payload
        for topic, payload in broker.retained.items()
        if "/codex/primary/remaining/state" in topic
    )
    assert codex_state == "25.0"

    restarted_bridge, _ = _bridge(broker, connect=True)
    assert restarted_bridge.inventory == inventory_before
    stale = restarted_bridge.publish_and_reconcile(
        _snapshot([_row("codex", "primary")]), complete=True
    )
    assert stale and all("claude" in topic for topic in stale)


def test_partial_snapshot_does_not_publish_untracked_dynamic_window():
    broker = FakeBroker()
    bridge, _ = _bridge(broker)
    bridge.publish_and_reconcile(_snapshot([_row("codex", "primary")]), complete=True)

    bridge.publish_and_reconcile(
        _snapshot(
            [_row("codex", "primary"), _row("codex", "weekly")],
            errors=[{"provider": "claude", "kind": "provider"}],
        ),
        complete=False,
    )
    bridge.publish_and_reconcile(_snapshot([_row("codex", "primary")]), complete=True)

    assert not any(
        "/weekly/" in topic or "codex_weekly" in topic for topic in broker.retained
    )


def test_snapshot_completeness_requires_success_status_and_no_provider_errors():
    from agent_telemetry import snapshot_is_complete

    healthy = _snapshot([_row("codex", "primary")])
    partial = _snapshot(
        [_row("codex", "primary")],
        errors=[{"provider": "claude", "kind": "provider"}],
    )

    assert snapshot_is_complete(healthy, 200) is True
    assert snapshot_is_complete(healthy, 302) is False
    assert snapshot_is_complete(healthy, 500) is False
    assert snapshot_is_complete(partial, 200) is False


def test_invalid_manifest_never_grants_deletion_authority():
    from agent_telemetry import manifest_topic

    broker = FakeBroker()
    manifest = manifest_topic("agent_telemetry", "test-host")
    foreign = "agent_telemetry/other_host/codex/primary/remaining/state"
    broker.retained[foreign] = "42"
    broker.retained[manifest] = json.dumps(
        {"schema_version": 1, "topics": [foreign]},
        separators=(",", ":"),
    )

    bridge, client = _bridge(broker, connect=True)
    assert bridge.manifest_received.is_set()
    assert bridge.inventory is None
    bridge.publish_and_reconcile(_snapshot([_row("codex", "primary")]), complete=True)

    assert foreign in broker.retained
    assert not _tombstones(client.calls)


@pytest.mark.parametrize("failure_stage", ["desired", "tombstone", "manifest"])
def test_failed_publication_never_advances_inventory(failure_stage):
    from agent_telemetry import manifest_topic

    class FailedMessageInfo(FakeMessageInfo):
        rc = 1

    class FailingClient(FakeMQTTClient):
        def __init__(self, broker, predicate):
            super().__init__(broker)
            self.predicate = predicate
            self.failed = False

        def publish(self, topic, payload=None, qos=0, retain=False, **kwargs):
            self.calls.append(
                {
                    "topic": topic,
                    "payload": payload,
                    "qos": qos,
                    "retain": retain,
                    **kwargs,
                }
            )
            if not self.failed and self.predicate(topic, payload):
                self.failed = True
                return FailedMessageInfo()
            return self.broker.publish(topic, payload, qos=qos, retain=retain)

    broker = FakeBroker()
    bridge, _ = _bridge(broker)
    initial = _snapshot([_row("codex", "primary"), _row("claude", "primary")])
    bridge.publish_and_reconcile(initial, complete=True)
    inventory_before = set(bridge.inventory)
    manifest = manifest_topic("agent_telemetry", "test-host")
    manifest_before = broker.retained[manifest]
    predicates = {
        "desired": lambda topic, payload: payload != "" and topic != manifest,
        "tombstone": lambda topic, payload: payload == "",
        "manifest": lambda topic, payload: topic == manifest,
    }
    failing = FailingClient(broker, predicates[failure_stage])
    bridge.client = failing

    with pytest.raises(RuntimeError, match="MQTT publish failed"):
        bridge.publish_and_reconcile(
            _snapshot([_row("codex", "primary")]),
            complete=True,
        )

    assert bridge.inventory == inventory_before
    assert broker.retained[manifest] == manifest_before
    if failure_stage == "desired":
        assert not _tombstones(failing.calls)


def test_bridge_restart_loads_manifest_and_reconciles_obsolete_topics():
    broker = FakeBroker()
    first_bridge, _ = _bridge(broker)
    first_bridge.publish_and_reconcile(
        _snapshot([_row("codex", "primary"), _row("claude", "primary")]),
        complete=True,
    )

    restarted_bridge, _ = _bridge(broker, connect=True)
    assert restarted_bridge.manifest_received.is_set()
    assert restarted_bridge.inventory
    stale = restarted_bridge.publish_and_reconcile(
        _snapshot([_row("codex", "primary")]),
        complete=True,
    )

    assert stale
    assert all("claude" in topic for topic in stale)
    assert not any("/claude/" in topic for topic in broker.retained)


def test_broker_restart_republishes_desired_topics_and_manifest():
    from agent_telemetry import manifest_topic, render_desired_topics

    broker = FakeBroker()
    snapshot = _snapshot([_row("codex", "primary")], credits=[_credit("codex")])
    first_bridge, _ = _bridge(broker)
    first_bridge.publish_and_reconcile(snapshot, complete=True)
    broker.restart()

    restarted_bridge, client = _bridge(broker, connect=True)
    restarted_bridge.latest = snapshot
    restarted_bridge.latest_complete = True
    assert restarted_bridge.republish_if_needed(manifest_timeout=0) is True

    desired = render_desired_topics(snapshot, host_id="test-host")
    manifest = manifest_topic("agent_telemetry", "test-host")
    assert set(broker.retained) == set(desired) | {manifest}
    assert all(call["qos"] == 1 and call["retain"] is True for call in client.calls)
    assert not _tombstones(client.calls)
