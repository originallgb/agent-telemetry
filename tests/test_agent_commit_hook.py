from tools.agent_commit_hook import ensure_dir, redact, write_json


def test_redacts_assignment_secret_shapes():
    assert redact('password = "mqtt-value"') == "password = [REDACTED]"
    assert redact("authorization: sensitive-value") == "authorization: [REDACTED]"
    assert redact('mqtt_password = "mqtt-value"') == "mqtt_password = [REDACTED]"
    assert redact('"password": "mqtt-value"') == '"password": [REDACTED]'


def test_redacts_credentials_embedded_in_url():
    assert (
        redact("https://operator:sensitive@example.test/path")
        == "https://[REDACTED]@example.test/path"
    )
    assert (
        redact("https://plain-token@example.test/path")
        == "https://[REDACTED]@example.test/path"
    )


def test_redacts_bearer_tokens():
    assert redact("Bearer abc.def-123") == "Bearer [REDACTED]"


def test_redacts_well_known_token_shapes():
    value = "ghp_" + "abcdefghijklmnopqrstuvwxyz" + "123456"
    assert redact(value) == "[REDACTED]"


def test_memory_storage_uses_private_permissions(tmp_path):
    directory = ensure_dir(tmp_path / "memory")
    report = directory / "report.json"
    write_json(report, {"result": "safe"})

    assert directory.stat().st_mode & 0o777 == 0o700
    assert report.stat().st_mode & 0o777 == 0o600
