import json

from conftest import FakeMQTTClient


def _publish(client, normalized):
    from agent_telemetry import publish_snapshot

    publish_snapshot(
        client,
        normalized,
        topic_prefix="agent_telemetry",
        discovery_prefix="homeassistant",
        host_id="test-host",
    )


def test_every_snapshot_message_is_retained(fake_client, normalized):
    _publish(fake_client, normalized)

    assert fake_client.calls
    assert all(call["retain"] is True for call in fake_client.calls)
    topics = {call["topic"] for call in fake_client.calls}
    assert any(topic.endswith("/config") for topic in topics)
    assert any(topic.endswith("/state") for topic in topics)
    assert any(topic.endswith("/attributes") for topic in topics)

    for call in fake_client.calls:
        if call["topic"].endswith("/config"):
            config = json.loads(call["payload"])
            assert "expire_after" not in config


def test_retained_snapshot_survives_collector_restart(fake_broker, normalized):
    first_collector = FakeMQTTClient(fake_broker)
    _publish(first_collector, normalized)
    first_snapshot = dict(fake_broker.retained)

    second_collector = FakeMQTTClient(fake_broker)
    _publish(second_collector, normalized)

    assert fake_broker.retained == first_snapshot
    assert second_collector.calls
    assert all(call["retain"] is True for call in second_collector.calls)


def test_collector_republishes_complete_snapshot_after_broker_restart(
    fake_broker, normalized
):
    collector = FakeMQTTClient(fake_broker)
    _publish(collector, normalized)
    expected_snapshot = dict(fake_broker.retained)
    assert expected_snapshot

    fake_broker.restart()
    assert fake_broker.retained == {}

    # Models the collector's reconnect/full-refresh path after the broker is up.
    reconnected_client = FakeMQTTClient(fake_broker)
    _publish(reconnected_client, normalized)

    assert fake_broker.retained == expected_snapshot
    assert all(call["retain"] is True for call in reconnected_client.calls)
