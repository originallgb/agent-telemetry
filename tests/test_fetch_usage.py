import io
import json
from copy import deepcopy
from urllib.error import HTTPError


def test_non_2xx_valid_body_is_parsed_and_healthy_rows_survive(monkeypatch, usage_payload):
    import agent_telemetry

    response_payload = deepcopy(usage_payload)
    response_payload.insert(
        1,
        {
            "provider": "broken-provider",
            "source": "oauth",
            "usage": None,
            "error": {
                "code": 42,
                "kind": "provider",
                "message": "synthetic upstream failure",
            },
        },
    )
    error_response = HTTPError(
        "http://127.0.0.1:8899/usage",
        503,
        "Service Unavailable",
        {},
        io.BytesIO(json.dumps(response_payload).encode()),
    )

    def non_2xx(*args, **kwargs):
        raise error_response

    monkeypatch.setattr(agent_telemetry, "urlopen", non_2xx)

    payload, status = agent_telemetry.fetch_usage(
        "http://127.0.0.1:8899/usage", timeout=0.1
    )
    result = agent_telemetry.normalize_usage(payload)

    assert status == 503
    assert {row["provider"] for row in result["rows"]} == {
        "codex",
        "claude",
        "antigravity",
    }
    assert result["errors"] == [
        {
            "provider": "broken-provider",
            "code": 42,
            "kind": "provider",
            "message": "synthetic upstream failure",
        }
    ]
