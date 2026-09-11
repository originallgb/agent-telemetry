import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def usage_payload():
    return json.loads((REPO_ROOT / "docs" / "serve-bare-usage.json").read_text())


@pytest.fixture
def normalized(usage_payload):
    from agent_telemetry import normalize_usage

    return normalize_usage(usage_payload, now="2026-08-27T22:05:00Z")


class FakeBroker:
    """Small retained-message store; no sockets or broker credentials involved."""

    def __init__(self):
        self.retained = {}
        self.generation = 0

    def publish(self, topic, payload, *, qos=0, retain=False):
        if retain:
            if payload in (None, b"", ""):
                self.retained.pop(topic, None)
            else:
                self.retained[topic] = payload
        return FakeMessageInfo()

    def restart(self):
        # A deliberately pessimistic restart: the in-memory retained database is
        # lost. The collector must be able to republish a complete snapshot.
        self.retained.clear()
        self.generation += 1


class FakeMessageInfo:
    rc = 0
    mid = 1

    def wait_for_publish(self, timeout=None):
        return None

    def is_published(self):
        return True


def topic_matches(filter_, topic):
    """Minimal MQTT filter match supporting + and #, enough for the fakes."""
    f = filter_.split("/")
    t = topic.split("/")
    for index, part in enumerate(f):
        if part == "#":
            return True
        if index >= len(t):
            return False
        if part != "+" and part != t[index]:
            return False
    return len(f) == len(t)


class FakeMessage:
    def __init__(self, topic, payload, retain=True):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload
        self.retain = retain


class FakeMQTTClient:
    def __init__(self, broker):
        self.broker = broker
        self.calls = []
        self.on_message = None
        self.subscriptions = []

    def subscribe(self, filter_, qos=0):
        """Replay the broker's retained store, the way a real broker does."""
        self.subscriptions.append(filter_)
        if self.on_message is None:
            return
        for topic, payload in sorted(self.broker.retained.items()):
            if topic_matches(filter_, topic):
                self.on_message(self, None, FakeMessage(topic, payload))

    def unsubscribe(self, filter_):
        if filter_ in self.subscriptions:
            self.subscriptions.remove(filter_)

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
        return self.broker.publish(topic, payload, qos=qos, retain=retain)


@pytest.fixture
def fake_broker():
    return FakeBroker()


@pytest.fixture
def fake_client(fake_broker):
    return FakeMQTTClient(fake_broker)
