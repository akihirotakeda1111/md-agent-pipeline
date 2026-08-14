from __future__ import annotations

import json
from datetime import UTC, datetime
from io import StringIO

import pytest
from agent.errors import AgentError, ErrorCategory
from agent.logger import REQUIRED_FIELDS, log_event


def test_log_event_writes_json_lines_with_required_fields() -> None:
    stream = StringIO()
    record = log_event(
        "config_loaded",
        "loaded default config",
        task_id="phase1-foundation",
        phase="foundation",
        state="PENDING",
        stream=stream,
        timestamp=datetime(2026, 8, 14, 3, 51, tzinfo=UTC),
    )

    line = stream.getvalue()
    assert line.endswith("\n")
    assert line.count("\n") == 1

    parsed = json.loads(line)
    assert parsed == record
    assert parsed["event"] == "config_loaded"
    assert parsed["task_id"] == "phase1-foundation"
    assert parsed["phase"] == "foundation"
    assert parsed["state"] == "PENDING"
    assert parsed["message"] == "loaded default config"
    assert parsed["timestamp"] == "2026-08-14T03:51:00Z"
    assert all(field in parsed for field in REQUIRED_FIELDS)


def test_log_event_allows_null_task_context() -> None:
    stream = StringIO()
    record = log_event("startup", "orchestrator start", stream=stream)

    assert record["task_id"] is None
    assert record["phase"] is None
    assert record["state"] is None
    assert "T" in record["timestamp"]


def test_log_event_rejects_empty_event() -> None:
    with pytest.raises(AgentError) as exc_info:
        log_event("", "missing event", stream=StringIO())

    assert exc_info.value.category is ErrorCategory.INVALID_INPUT


def test_log_event_rejects_required_field_collision() -> None:
    with pytest.raises(AgentError) as exc_info:
        log_event(
            "startup",
            "collide",
            stream=StringIO(),
            extra={"event": "other"},
        )

    assert exc_info.value.category is ErrorCategory.INVALID_INPUT
