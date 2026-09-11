#!/usr/bin/env python3
"""Bridge CodexBar's cached usage payload to retained Home Assistant MQTT sensors."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import re
import signal
import socket
import stat
import threading
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


LOG = logging.getLogger("agent_telemetry")
DEFAULT_CONFIG = Path("~/.config/agent-telemetry/config.toml").expanduser()
WINDOW_META_KEYS = {
    "accountEmail",
    "codexResetCredits",
    "dataConfidence",
    "extraRateWindows",
    "identity",
    "loginMethod",
    "updatedAt",
}
PACE_KEYS = {
    "stage",
    "deltaPercent",
    "expectedUsedPercent",
    "willLastToReset",
    "etaSeconds",
    "summary",
}


def _slug(value: Any) -> str:
    original = str(value).strip()
    slug = re.sub(r"[^a-z0-9]+", "_", original.lower()).strip("_") or "unknown"
    if original.lower() != slug:
        digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        slug = f"{slug}_{digest}"
    return slug[:120]


def _remaining_percent(used_percent: Any) -> float:
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        raise ValueError("usedPercent must be numeric")
    used = float(used_percent)
    if not math.isfinite(used):
        raise ValueError("usedPercent must be finite")
    return round(max(0.0, min(100.0, 100.0 - used)), 6)


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _window_minutes(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)) or value <= 0 or int(value) != value:
        return None
    return int(value)


def _clean_pace(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key in PACE_KEYS:
        item = value.get(key)
        if key in {"stage", "summary"}:
            if isinstance(item, str):
                cleaned[key] = item[:512 if key == "summary" else 64]
        elif key == "willLastToReset":
            if isinstance(item, bool):
                cleaned[key] = item
        elif isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)):
            cleaned[key] = item
    return cleaned or None


def _window_signature(window: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        window.get("windowMinutes"),
        window.get("resetsAt"),
        window.get("usedPercent"),
    )


def _window_title(window_id: str, window: dict[str, Any]) -> str:
    minutes = window.get("windowMinutes")
    if minutes == 300:
        return "5-hour"
    if minutes == 10080:
        return "Weekly"
    if window_id in {"primary", "secondary", "tertiary"}:
        return window_id.title()
    return window_id.replace("_", " ").replace("-", " ").title()


def normalize_usage(payload: Any, now: Any = None) -> dict[str, list[dict[str, Any]]]:
    """Normalize provider-shaped CodexBar output without assuming fixed buckets."""
    del now  # Reserved for future freshness annotations; never drives availability.
    if not isinstance(payload, list):
        raise ValueError("CodexBar /usage payload must be a JSON array")

    rows: list[dict[str, Any]] = []
    reset_credits: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for provider_row in payload:
        if not isinstance(provider_row, dict):
            errors.append({"provider": "unknown", "message": "provider row is not an object"})
            continue

        provider = str(provider_row.get("provider") or "unknown")
        error = provider_row.get("error")
        usage = provider_row.get("usage")
        if error or not isinstance(usage, dict):
            detail = error if isinstance(error, dict) else {}
            errors.append(
                {
                    "provider": provider,
                    **{key: detail[key] for key in ("code", "kind", "message") if key in detail},
                    **({"message": "provider usage is unavailable"} if not detail else {}),
                }
            )
            continue

        base = {
            "provider": provider,
            "account": provider_row.get("account"),
            "source": provider_row.get("source"),
            "data_confidence": usage.get("dataConfidence"),
            "updated_at": usage.get("updatedAt"),
        }
        pace_by_window = provider_row.get("pace")
        if not isinstance(pace_by_window, dict):
            pace_by_window = {}

        extras = usage.get("extraRateWindows")
        extra_signatures: set[tuple[Any, Any, Any]] = set()
        seen_extra_ids: set[str] = set()
        if isinstance(extras, list):
            for extra in extras:
                if not isinstance(extra, dict) or not isinstance(extra.get("window"), dict):
                    continue
                window = extra["window"]
                if "usedPercent" not in window:
                    continue
                window_id = str(extra.get("id") or extra.get("title") or "extra")
                if window_id in seen_extra_ids:
                    continue
                seen_extra_ids.add(window_id)
                try:
                    remaining = _remaining_percent(window["usedPercent"])
                except ValueError:
                    errors.append({"provider": provider, "kind": "invalid_window"})
                    continue
                signature = _window_signature(window)
                try:
                    extra_signatures.add(signature)
                except TypeError:
                    errors.append({"provider": provider, "kind": "invalid_window"})
                    continue
                rows.append(
                    {
                        **base,
                        "window_id": window_id,
                        "title": str(extra.get("title") or _window_title(window_id, window)),
                        "remaining_percent": remaining,
                        "resets_at": _timestamp(window.get("resetsAt")),
                        "window_minutes": _window_minutes(window.get("windowMinutes")),
                        "pace": None,
                    }
                )

        for window_id, window in usage.items():
            if window_id in WINDOW_META_KEYS or not isinstance(window, dict):
                continue
            if "usedPercent" not in window:
                continue
            if window_id in {"primary", "secondary", "tertiary"}:
                try:
                    if _window_signature(window) in extra_signatures:
                        continue
                except TypeError:
                    errors.append({"provider": provider, "kind": "invalid_window"})
                    continue
            try:
                remaining = _remaining_percent(window["usedPercent"])
            except ValueError:
                errors.append({"provider": provider, "kind": "invalid_window"})
                continue
            pace = _clean_pace(pace_by_window.get(window_id))
            rows.append(
                {
                    **base,
                    "window_id": str(window_id),
                    "title": _window_title(str(window_id), window),
                    "remaining_percent": remaining,
                    "resets_at": _timestamp(window.get("resetsAt")),
                    "window_minutes": _window_minutes(window.get("windowMinutes")),
                    "pace": pace,
                }
            )

        credits = usage.get("codexResetCredits")
        available_count = credits.get("availableCount") if isinstance(credits, dict) else None
        if (
            isinstance(available_count, int)
            and not isinstance(available_count, bool)
            and available_count >= 0
        ):
            reset_credits.append(
                {
                    "provider": provider,
                    "available_count": available_count,
                    "updated_at": _timestamp(credits.get("updatedAt")),
                }
            )

    return {"rows": rows, "reset_credits": reset_credits, "errors": errors}


def _sanitized_error(error: dict[str, Any]) -> dict[str, Any]:
    """Return bounded diagnostics without forwarding provider-controlled text."""
    return {
        key: error[key]
        for key in ("provider", "code", "kind")
        if key in error and isinstance(error[key], (str, int, float))
    }


def _device(host_id: str) -> dict[str, Any]:
    return {
        "identifiers": [f"agent_telemetry_{_slug(host_id)}"],
        "name": f"Agent telemetry ({host_id})",
        "manufacturer": "agent-telemetry",
        "model": "CodexBar MQTT bridge",
    }


def _serialize_payload(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _publish_retained(client: Any, topic: str, payload: Any) -> None:
    payload = _serialize_payload(payload)
    result = client.publish(topic, payload=payload, qos=1, retain=True)
    if getattr(result, "rc", 0) != 0:
        raise RuntimeError(f"MQTT publish failed with rc={result.rc}")
    if hasattr(result, "wait_for_publish"):
        result.wait_for_publish(timeout=10)
    if hasattr(result, "is_published") and not result.is_published():
        raise RuntimeError("MQTT publish did not complete")


def manifest_topic(topic_prefix: str, host_id: str) -> str:
    """Return the private retained inventory topic for one configured host."""
    return f"{topic_prefix}/{_slug(host_id)}/_bridge/inventory"


def _owned_topics(
    topics: Any,
    *,
    topic_prefix: str,
    discovery_prefix: str,
    host_slug: str,
) -> set[str]:
    """Return exact rendered topics that this configured bridge may retract."""
    state_root = f"{topic_prefix}/{host_slug}/"
    config_root = f"{discovery_prefix}/sensor/agent_telemetry_{host_slug}_"
    inventory_topic = manifest_topic(topic_prefix, host_slug)
    owned: set[str] = set()
    safe_segment = re.compile(r"[a-z0-9_]+").fullmatch
    for topic in topics:
        if not isinstance(topic, str) or topic == inventory_topic:
            continue
        if topic.startswith(state_root):
            parts = topic[len(state_root) :].split("/")
            if (
                len(parts) == 3
                and safe_segment(parts[0])
                and safe_segment(parts[1])
                and parts[2] == "attributes"
            ) or (
                len(parts) == 4
                and safe_segment(parts[0])
                and safe_segment(parts[1])
                and parts[2:] in (["remaining", "state"], ["reset", "state"])
            ) or (
                len(parts) == 3
                and safe_segment(parts[0])
                and parts[1] == "reset_credits"
                and parts[2] in {"state", "attributes"}
            ):
                owned.add(topic)
        elif topic.startswith(config_root) and topic.endswith("/config"):
            entity_id = topic[len(config_root) : -len("/config")]
            if entity_id and re.fullmatch(r"[a-z0-9_]+", entity_id):
                owned.add(topic)
    return owned


def _manifest_payload(topics: Any) -> str:
    return _serialize_payload({"schema_version": 1, "topics": sorted(topics)})


def _decode_manifest(
    payload: bytes | str,
    *,
    topic_prefix: str,
    discovery_prefix: str,
    host_slug: str,
) -> set[str] | None:
    """Validate deletion authority from the bridge's private retained manifest."""
    if isinstance(payload, bytes):
        if len(payload) > 1_000_000:
            return None
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return None
    topics = document.get("topics")
    if (
        not isinstance(topics, list)
        or len(topics) > 10_000
        or any(not isinstance(topic, str) or len(topic) > 512 for topic in topics)
    ):
        return None
    owned = _owned_topics(
        topics,
        topic_prefix=topic_prefix,
        discovery_prefix=discovery_prefix,
        host_slug=host_slug,
    )
    return owned if len(owned) == len(topics) == len(set(topics)) else None


def render_desired_topics(
    rows: dict[str, list[dict[str, Any]]],
    *,
    topic_prefix: str = "agent_telemetry",
    discovery_prefix: str = "homeassistant",
    host_id: str | None = None,
) -> dict[str, str]:
    """Purely render a normalized snapshot into its complete retained topic map."""
    host_id = host_id or socket.gethostname().split(".")[0]
    host_slug = _slug(host_id)
    device = _device(host_id)
    desired: dict[str, str] = {}

    def add(topic: str, payload: Any) -> None:
        desired[topic] = _serialize_payload(payload)

    for row in rows.get("rows", []):
        provider = _slug(row["provider"])
        window_id = _slug(row["window_id"])
        base_topic = f"{topic_prefix}/{host_slug}/{provider}/{window_id}"
        entity_base = f"agent_telemetry_{host_slug}_{provider}_{window_id}"
        remaining_id = f"{entity_base}_remaining"
        remaining_state = f"{base_topic}/remaining/state"
        attributes_topic = f"{base_topic}/attributes"
        provider_name = str(row["provider"]).replace("-", " ").title()

        config = {
            "name": f"{provider_name} {row['title']} remaining",
            "unique_id": remaining_id,
            "state_topic": remaining_state,
            "json_attributes_topic": attributes_topic,
            "device_class": "battery",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "device": device,
        }
        add(f"{discovery_prefix}/sensor/{remaining_id}/config", config)
        add(remaining_state, str(row["remaining_percent"]))
        attributes = {
            **(_clean_pace(row.get("pace")) or {}),
            "provider": row["provider"],
            "window_id": row["window_id"],
            "window_minutes": row.get("window_minutes"),
            "resets_at": row.get("resets_at"),
            "data_confidence": row.get("data_confidence"),
            "source": row.get("source"),
            "updated_at": row.get("updated_at"),
        }
        add(attributes_topic, attributes)

        if row.get("resets_at"):
            reset_id = f"{entity_base}_reset"
            reset_state = f"{base_topic}/reset/state"
            reset_config = {
                "name": f"{provider_name} {row['title']} reset",
                "unique_id": reset_id,
                "state_topic": reset_state,
                "device_class": "timestamp",
                "device": device,
            }
            add(f"{discovery_prefix}/sensor/{reset_id}/config", reset_config)
            add(reset_state, row["resets_at"])

    for credit in rows.get("reset_credits", []):
        provider = _slug(credit["provider"])
        entity_id = f"agent_telemetry_{host_slug}_{provider}_reset_credits"
        state_topic = f"{topic_prefix}/{host_slug}/{provider}/reset_credits/state"
        attributes_topic = f"{topic_prefix}/{host_slug}/{provider}/reset_credits/attributes"
        config = {
            "name": f"{str(credit['provider']).title()} reset credits",
            "unique_id": entity_id,
            "state_topic": state_topic,
            "json_attributes_topic": attributes_topic,
            "state_class": "measurement",
            "icon": "mdi:backup-restore",
            "device": device,
        }
        add(f"{discovery_prefix}/sensor/{entity_id}/config", config)
        add(state_topic, str(credit["available_count"]))
        add(attributes_topic, {"updated_at": credit.get("updated_at")})

    return desired


def publish_snapshot(
    client: Any,
    rows: dict[str, list[dict[str, Any]]],
    *,
    topic_prefix: str = "agent_telemetry",
    discovery_prefix: str = "homeassistant",
    host_id: str | None = None,
) -> None:
    """Publish one complete normalized snapshot as retained MQTT messages."""
    desired = render_desired_topics(
        rows,
        topic_prefix=topic_prefix,
        discovery_prefix=discovery_prefix,
        host_id=host_id,
    )
    for topic, payload in desired.items():
        _publish_retained(client, topic, payload)


def fetch_usage(url: str, timeout: float = 20.0) -> tuple[Any, int]:
    """Parse JSON even on non-2xx responses so valid partial rows survive."""
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            status_code = response.status
            body = response.read()
    except HTTPError as exc:
        status_code = exc.code
        body = exc.read()
    except URLError as exc:
        raise RuntimeError(f"CodexBar request failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError("CodexBar request timed out") from exc

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"CodexBar returned invalid JSON (HTTP {status_code})") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"CodexBar returned a non-array payload (HTTP {status_code})")
    return payload, status_code


def snapshot_is_complete(snapshot: dict[str, Any], status_code: int) -> bool:
    """Only a successful response with no provider errors may delete retained topics."""
    return 200 <= status_code < 300 and not snapshot.get("errors")


@dataclass(frozen=True)
class Settings:
    codexbar_url: str
    poll_interval: float
    request_timeout: float
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_client_id: str
    topic_prefix: str
    discovery_prefix: str
    host_id: str


def _topic_prefix(value: Any, name: str) -> str:
    prefix = str(value).strip("/")
    parts = prefix.split("/")
    if not prefix or any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise ValueError(f"{name} must contain only safe MQTT topic segments")
    return prefix


def load_settings(path: Path) -> Settings:
    path = path.expanduser()
    data = tomllib.loads(path.read_text())
    codexbar = data.get("codexbar", {})
    mqtt = data.get("mqtt", {})
    telemetry = data.get("telemetry", {})
    mode = stat.S_IMODE(path.stat().st_mode)
    if mqtt.get("password") and mode & 0o077:
        raise PermissionError(f"Config {path} contains an MQTT password; use chmod 600")
    if mode & 0o077:
        LOG.warning("Config %s is readable by group/others; use chmod 600", path)
    mqtt_password = os.environ.get("AGENT_TELEMETRY_MQTT_PASSWORD", mqtt.get("password"))
    codexbar_url = str(
        os.environ.get(
            "CODEXBAR_URL", codexbar.get("url", "http://127.0.0.1:8899/usage")
        )
    )
    parsed_url = urlsplit(codexbar_url)
    try:
        parsed_port = parsed_url.port
    except ValueError as exc:
        raise ValueError("CodexBar URL has an invalid port") from exc
    if not (
        parsed_url.scheme == "http"
        and parsed_url.hostname == "127.0.0.1"
        and parsed_url.path == "/usage"
        and not parsed_url.query
        and not parsed_url.fragment
        and parsed_url.username is None
        and parsed_url.password is None
        and (parsed_port is None or 1 <= parsed_port <= 65535)
    ):
        raise ValueError(
            "CodexBar URL must be bare loopback http://127.0.0.1:<port>/usage"
        )
    poll_interval = float(codexbar.get("poll_interval_seconds", 30))
    request_timeout = float(codexbar.get("request_timeout_seconds", 40))
    mqtt_port = int(mqtt.get("port", 1883))
    if not math.isfinite(poll_interval) or not 1 <= poll_interval <= 3600:
        raise ValueError("poll_interval_seconds must be between 1 and 3600")
    if not math.isfinite(request_timeout) or not 1 <= request_timeout <= 300:
        raise ValueError("request_timeout_seconds must be between 1 and 300")
    if not 1 <= mqtt_port <= 65535:
        raise ValueError("MQTT port must be between 1 and 65535")
    if not str(mqtt.get("host", "")).strip():
        raise ValueError("MQTT host is required")
    return Settings(
        codexbar_url=codexbar_url,
        poll_interval=poll_interval,
        request_timeout=request_timeout,
        mqtt_host=str(mqtt["host"]),
        mqtt_port=mqtt_port,
        mqtt_username=mqtt.get("username"),
        mqtt_password=mqtt_password,
        mqtt_client_id=str(mqtt.get("client_id", f"agent-telemetry-{_slug(socket.gethostname())}")),
        topic_prefix=_topic_prefix(telemetry.get("topic_prefix", "agent_telemetry"), "topic_prefix"),
        discovery_prefix=_topic_prefix(
            telemetry.get("discovery_prefix", "homeassistant"), "discovery_prefix"
        ),
        host_id=str(telemetry.get("host_id", socket.gethostname().split(".")[0])),
    )


class Bridge:
    def __init__(self, client: Any, settings: Settings):
        self.client = client
        self.settings = settings
        self.latest: dict[str, list[dict[str, Any]]] | None = None
        self.latest_complete = False
        self.inventory: set[str] | None = None
        self.manifest_received = threading.Event()
        self.connected = threading.Event()
        self.republish_requested = threading.Event()
        self._lock = threading.Lock()

    def on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any = None) -> None:
        del userdata, flags, properties
        code_value = getattr(reason_code, "value", reason_code)
        if int(code_value) != 0:
            LOG.error("MQTT connection rejected: %s", reason_code)
            return
        LOG.info("MQTT connected")
        self.manifest_received.clear()
        if hasattr(client, "subscribe"):
            client.subscribe(self._manifest_topic(), qos=1)
        self.connected.set()
        self.republish_requested.set()

    def on_disconnect(self, client: Any, userdata: Any, disconnect_flags: Any, reason_code: Any, properties: Any = None) -> None:
        del client, userdata, disconnect_flags, properties
        self.connected.clear()
        LOG.warning("MQTT disconnected: %s", reason_code)

    def on_message(self, client: Any, userdata: Any, message: Any) -> None:
        del client, userdata
        if message.topic != self._manifest_topic() or not getattr(message, "retain", False):
            return
        inventory = _decode_manifest(
            message.payload,
            topic_prefix=self.settings.topic_prefix,
            discovery_prefix=self.settings.discovery_prefix,
            host_slug=self._host_slug(),
        )
        with self._lock:
            self.inventory = inventory
        if inventory is None:
            LOG.error("Ignoring invalid retained bridge inventory manifest")
        self.manifest_received.set()

    def _host_slug(self) -> str:
        return _slug(self.settings.host_id)

    def _manifest_topic(self) -> str:
        return manifest_topic(self.settings.topic_prefix, self.settings.host_id)

    def publish_and_reconcile(
        self,
        snapshot: dict[str, list[dict[str, Any]]],
        *,
        complete: bool | None = None,
    ) -> set[str]:
        """Publish desired topics and safely reconcile a confirmed healthy snapshot."""
        if complete is None:
            complete = not snapshot.get("errors")
        elif snapshot.get("errors"):
            complete = False
        desired = render_desired_topics(
            snapshot,
            topic_prefix=self.settings.topic_prefix,
            discovery_prefix=self.settings.discovery_prefix,
            host_id=self.settings.host_id,
        )
        with self._lock:
            previous = None if self.inventory is None else set(self.inventory)
        if not complete:
            # A partial response may refresh only entities whose ownership is
            # already recorded. New windows cannot be safely reconciled until
            # a complete response establishes their manifest authority.
            desired = (
                {
                    topic: payload
                    for topic, payload in desired.items()
                    if topic in previous
                }
                if previous is not None
                else {}
            )
        for topic, payload in desired.items():
            _publish_retained(self.client, topic, payload)

        if not complete:
            return set()

        stale = set() if previous is None else previous - desired.keys()
        for topic in sorted(stale, key=lambda t: (not t.endswith("/config"), t)):
            LOG.info("Tombstoning obsolete retained topic %s", topic)
            _publish_retained(self.client, topic, "")

        inventory = set(desired)
        _publish_retained(self.client, self._manifest_topic(), _manifest_payload(inventory))
        with self._lock:
            self.inventory = inventory
        return stale

    def publish(
        self,
        snapshot: dict[str, list[dict[str, Any]]],
        *,
        complete: bool | None = None,
    ) -> None:
        if complete is None:
            complete = not snapshot.get("errors")
        self.cache_snapshot(snapshot, complete=complete)
        try:
            self.publish_and_reconcile(snapshot, complete=complete)
        except Exception:
            self.republish_requested.set()
            raise

    def cache_snapshot(
        self,
        snapshot: dict[str, list[dict[str, Any]]],
        *,
        complete: bool,
    ) -> None:
        """Cache a snapshot without forgetting entities omitted by a partial response."""
        with self._lock:
            if complete or self.latest is None:
                self.latest = snapshot
            else:
                merged = {
                    "rows": list(self.latest.get("rows", [])),
                    "reset_credits": list(self.latest.get("reset_credits", [])),
                    "errors": list(snapshot.get("errors", [])),
                }
                row_indexes = {
                    (row.get("provider"), row.get("window_id")): index
                    for index, row in enumerate(merged["rows"])
                }
                for row in snapshot.get("rows", []):
                    key = (row.get("provider"), row.get("window_id"))
                    if key in row_indexes:
                        merged["rows"][row_indexes[key]] = row
                    else:
                        row_indexes[key] = len(merged["rows"])
                        merged["rows"].append(row)
                credit_indexes = {
                    credit.get("provider"): index
                    for index, credit in enumerate(merged["reset_credits"])
                }
                for credit in snapshot.get("reset_credits", []):
                    provider = credit.get("provider")
                    if provider in credit_indexes:
                        merged["reset_credits"][credit_indexes[provider]] = credit
                    else:
                        credit_indexes[provider] = len(merged["reset_credits"])
                        merged["reset_credits"].append(credit)
                self.latest = merged
            self.latest_complete = complete

    def republish_if_needed(self, *, manifest_timeout: float = 1.0) -> bool:
        """Republish cached state outside Paho's network callback thread."""
        if not self.connected.is_set() or not self.republish_requested.is_set():
            return False
        self.manifest_received.wait(timeout=manifest_timeout)
        with self._lock:
            snapshot = self.latest
            complete = self.latest_complete
            inventory = None if self.inventory is None else set(self.inventory)
        if snapshot is None:
            return False
        self.publish_and_reconcile(snapshot, complete=complete)
        if not complete and inventory is not None:
            _publish_retained(
                self.client,
                self._manifest_topic(),
                _manifest_payload(inventory),
            )
        self.republish_requested.clear()
        return True


def build_client(settings: Settings, bridge: Bridge | None = None) -> tuple[Any, Bridge]:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise RuntimeError("paho-mqtt is not installed; run uv sync") from exc
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=settings.mqtt_client_id)
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
    result_bridge = bridge or Bridge(client, settings)
    result_bridge.client = client
    client.on_connect = result_bridge.on_connect
    client.on_disconnect = result_bridge.on_disconnect
    client.on_message = result_bridge.on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client, result_bridge


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--once", action="store_true", help="Publish one snapshot and exit")
    parser.add_argument("--validate", action="store_true", help="Validate config and CodexBar without MQTT")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings(args.config)
    payload, status_code = fetch_usage(settings.codexbar_url, settings.request_timeout)
    snapshot = normalize_usage(payload)
    if status_code >= 400:
        LOG.warning("CodexBar returned HTTP %d; publishing parsed healthy rows", status_code)
    for error in snapshot["errors"]:
        LOG.warning("Provider error: %s", json.dumps(_sanitized_error(error), sort_keys=True))
    if args.validate:
        print(
            json.dumps(
                {
                    "http_status": status_code,
                    "window_rows": len(snapshot["rows"]),
                    "reset_credit_rows": len(snapshot["reset_credits"]),
                    "provider_errors": [_sanitized_error(error) for error in snapshot["errors"]],
                },
                indent=2,
            )
        )
        return 0

    client, bridge = build_client(settings)
    bridge.cache_snapshot(snapshot, complete=snapshot_is_complete(snapshot, status_code))
    client.connect(settings.mqtt_host, settings.mqtt_port, keepalive=60)
    client.loop_start()
    if not bridge.connected.wait(timeout=15):
        client.loop_stop()
        raise RuntimeError("MQTT connection timed out")
    bridge.republish_if_needed()
    if args.once:
        client.disconnect()
        client.loop_stop()
        return 0

    stopped = threading.Event()

    def stop(signum: int, frame: Any) -> None:
        del signum, frame
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_poll = time.monotonic() + settings.poll_interval
    while not stopped.is_set():
        if bridge.republish_requested.is_set():
            try:
                bridge.republish_if_needed()
            except Exception:
                LOG.exception("MQTT reconnect republish failed")
        now = time.monotonic()
        if now >= next_poll:
            next_poll = now + settings.poll_interval
            try:
                payload, status_code = fetch_usage(settings.codexbar_url, settings.request_timeout)
                snapshot = normalize_usage(payload)
                if status_code >= 400:
                    LOG.warning("CodexBar returned HTTP %d; publishing parsed healthy rows", status_code)
                for error in snapshot["errors"]:
                    LOG.warning(
                        "Provider error: %s",
                        json.dumps(_sanitized_error(error), sort_keys=True),
                    )
                if bridge.connected.is_set():
                    bridge.publish(
                        snapshot,
                        complete=snapshot_is_complete(snapshot, status_code),
                    )
                else:
                    bridge.cache_snapshot(
                        snapshot,
                        complete=snapshot_is_complete(snapshot, status_code),
                    )
            except Exception:
                LOG.exception("Snapshot refresh failed; retained last-good state is unchanged")
        stopped.wait(min(1.0, max(0.0, next_poll - time.monotonic())))

    client.disconnect()
    client.loop_stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
