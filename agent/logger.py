"""JSON Lines structured event logger."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any, TextIO

from agent.errors import AgentError

REQUIRED_FIELDS = ("event", "task_id", "phase", "state", "message", "timestamp")


def utc_timestamp(now: datetime | None = None) -> str:
    instant = now or datetime.now(UTC)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def log_event(
    event: str,
    message: str,
    *,
    task_id: str | None = None,
    phase: str | None = None,
    state: str | None = None,
    stream: TextIO | None = None,
    timestamp: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one JSONL event and return the record.

    `phase` and `state` are separate fields so later state-machine logs stay queryable.
    """
    if not event:
        raise AgentError.invalid_input("event is required")
    if not message:
        raise AgentError.invalid_input("message is required")

    record: dict[str, Any] = {
        "event": event,
        "task_id": task_id,
        "phase": phase,
        "state": state,
        "message": message,
        "timestamp": utc_timestamp(timestamp),
    }
    if extra:
        for key in extra:
            if key in record:
                raise AgentError.invalid_input(f"extra field collides with required field: {key}")
        record.update(extra)

    output = stream or sys.stdout
    output.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    output.flush()
    return record
