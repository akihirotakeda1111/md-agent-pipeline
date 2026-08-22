"""Structured observability event names for Phase 6 and Phase 7."""

from __future__ import annotations

import re
from typing import Any, TextIO

from agent.errors import AgentError
from agent.logger import log_event

SPEC_DISCOVERED = "SPEC_DISCOVERED"
SPEC_VALIDATED = "SPEC_VALIDATED"
STATE_INITIALIZED = "STATE_INITIALIZED"
TASK_STARTED = "TASK_STARTED"
CODEX_STARTED = "CODEX_STARTED"
CODEX_COMPLETED = "CODEX_COMPLETED"
SCOPE_CHECK_STARTED = "SCOPE_CHECK_STARTED"
SCOPE_CHECK_PASSED = "SCOPE_CHECK_PASSED"
SCOPE_VIOLATION = "SCOPE_VIOLATION"
VALIDATION_STARTED = "VALIDATION_STARTED"
VALIDATION_PASSED = "VALIDATION_PASSED"
VALIDATION_FAILED = "VALIDATION_FAILED"
REPAIR_STARTED = "REPAIR_STARTED"
TASK_COMPLETED = "TASK_COMPLETED"
FINAL_VALIDATION_STARTED = "FINAL_VALIDATION_STARTED"
FINAL_VALIDATION_PASSED = "FINAL_VALIDATION_PASSED"
PR_CREATED = "PR_CREATED"
DELIVERY_VALIDATION_STARTED = "DELIVERY_VALIDATION_STARTED"
DELIVERY_VALIDATION_PASSED = "DELIVERY_VALIDATION_PASSED"
ESCALATED = "ESCALATED"
FAILED = "FAILED"
WORKFLOW_COMPLETED = "WORKFLOW_COMPLETED"
NOTIFICATION_FAILED = "NOTIFICATION_FAILED"
REVIEW_RECEIVED = "REVIEW_RECEIVED"
REVIEW_FILTERED = "REVIEW_FILTERED"
REVIEW_COLLECTED = "REVIEW_COLLECTED"
REVIEW_CLASSIFIED = "REVIEW_CLASSIFIED"
REVIEW_POLICY_APPLIED = "REVIEW_POLICY_APPLIED"
REVIEW_FIX_STARTED = "REVIEW_FIX_STARTED"
REVIEW_FIX_VALIDATION_PASSED = "REVIEW_FIX_VALIDATION_PASSED"
REVIEW_FIX_VALIDATION_FAILED = "REVIEW_FIX_VALIDATION_FAILED"
REVIEW_ESCALATED = "REVIEW_ESCALATED"
READY_FOR_HUMAN = "READY_FOR_HUMAN"
NOTIFICATION_FAILED_MESSAGE = "failure notification could not be published"
_MAX_DIAGNOSTIC_VALUE = 240
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+|token\s*[:=]\s*)\S+"
)


def emit(
    event: str,
    message: str,
    *,
    task_id: str | None = None,
    phase: str | None = "git-pr",
    state: str | None = None,
    stream: TextIO | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return log_event(
        event,
        message,
        task_id=task_id,
        phase=phase,
        state=state,
        stream=stream,
        extra=extra,
    )


def notification_error_code(error: BaseException) -> str:
    if isinstance(error, AgentError) and error.code:
        return _bounded_diagnostic_text(error.code)
    return _bounded_diagnostic_text(type(error).__name__)


def emit_notification_failed(
    *,
    phase: str,
    task_id: str | None,
    primary_outcome: str,
    primary_code: str | None,
    operation: str,
    error: BaseException,
    message: str = NOTIFICATION_FAILED_MESSAGE,
) -> dict[str, Any]:
    extra = {
        "primary_outcome": _bounded_diagnostic_text(primary_outcome),
        "primary_code": None if primary_code is None else _bounded_diagnostic_text(primary_code),
        "notification_operation": _bounded_diagnostic_text(operation),
        "notification_error_code": notification_error_code(error),
    }
    return emit(
        NOTIFICATION_FAILED,
        _bounded_diagnostic_text(message) or NOTIFICATION_FAILED_MESSAGE,
        task_id=task_id,
        phase=phase,
        extra=extra,
    )


def emit_notification_failed_best_effort(
    *,
    phase: str,
    task_id: str | None,
    primary_outcome: str,
    primary_code: str | None,
    operation: str,
    error: BaseException,
    message: str = NOTIFICATION_FAILED_MESSAGE,
) -> None:
    try:
        emit_notification_failed(
            phase=phase,
            task_id=task_id,
            primary_outcome=primary_outcome,
            primary_code=primary_code,
            operation=operation,
            error=error,
            message=message,
        )
    except Exception:
        return


def _bounded_diagnostic_text(value: str) -> str:
    redacted = _SECRET_PATTERN.sub(r"\1[redacted]", value)
    if len(redacted) <= _MAX_DIAGNOSTIC_VALUE:
        return redacted
    return redacted[:_MAX_DIAGNOSTIC_VALUE]
