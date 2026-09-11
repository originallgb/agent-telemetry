from copy import deepcopy


def _rows_for(result, provider):
    return [row for row in result["rows"] if row["provider"] == provider]


def _row(result, provider, window_id):
    return next(
        row
        for row in result["rows"]
        if row["provider"] == provider and row["window_id"] == window_id
    )


def test_normalizes_dynamic_antigravity_windows_without_inventing_pace(normalized):
    rows = _rows_for(normalized, "antigravity")

    assert {row["window_id"] for row in rows} == {
        "antigravity-quota-summary-gemini-5h",
        "antigravity-quota-summary-gemini-weekly",
        "antigravity-quota-summary-3p-5h",
        "antigravity-quota-summary-3p-weekly",
    }
    assert len(rows) == 4, "primary/secondary aliases must not duplicate extra windows"
    assert all(row["pace"] is None for row in rows)

    gemini_5h = _row(
        normalized, "antigravity", "antigravity-quota-summary-gemini-5h"
    )
    assert gemini_5h["title"] == "Gemini 5-hour"
    assert gemini_5h["window_minutes"] == 300
    assert gemini_5h["remaining_percent"] == 98.31649
    assert gemini_5h["resets_at"] == "2026-08-28T01:49:22Z"


def test_preserves_claude_percent_only_confidence_and_pace(normalized):
    rows = _rows_for(normalized, "claude")

    assert len(rows) == 2
    assert {row["data_confidence"] for row in rows} == {"percentOnly"}
    primary = _row(normalized, "claude", "primary")
    assert primary["remaining_percent"] == 7
    assert primary["pace"] == {
        "expectedUsedPercent": 61,
        "willLastToReset": False,
        "deltaPercent": 32,
        "etaSeconds": 833,
        "stage": "farAhead",
        "summary": "32% in deficit | Expected 61% used | Projected empty in 14m",
    }
    assert primary["account"] is None
    assert primary["source"] == "claude"
    assert primary["updated_at"] == "2026-08-27T22:04:23Z"


def test_discovers_windows_instead_of_using_a_fixed_bucket_list(usage_payload):
    from agent_telemetry import normalize_usage

    payload = deepcopy(usage_payload)
    claude = next(item for item in payload if item["provider"] == "claude")
    claude["usage"]["monthly"] = {
        "windowMinutes": 43200,
        "usedPercent": 12.5,
        "resetsAt": "2026-09-27T00:00:00Z",
    }
    claude["pace"]["monthly"] = {"stage": "onTrack", "deltaPercent": 0}

    result = normalize_usage(payload, now="2026-08-27T22:05:00Z")
    monthly = _row(result, "claude", "monthly")
    assert monthly["window_minutes"] == 43200
    assert monthly["remaining_percent"] == 87.5
    assert monthly["pace"] == {"stage": "onTrack", "deltaPercent": 0}


def test_surfaces_codex_reset_credit_count(normalized):
    assert normalized["reset_credits"] == [
        {
            "provider": "codex",
            "available_count": 1,
            "updated_at": "2026-08-27T22:04:17Z",
        }
    ]


def test_provider_error_is_isolated_while_healthy_rows_are_kept(usage_payload):
    from agent_telemetry import normalize_usage

    payload = deepcopy(usage_payload)
    payload.insert(
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

    # The normalizer receives the parsed body, independent of whether the HTTP
    # request was non-200 or a CLI process exited non-zero.
    result = normalize_usage(payload, now="2026-08-27T22:05:00Z")

    assert len(result["rows"]) == 8
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
