from __future__ import annotations

from agent.classify import FailureClass, classify_output, classify_validation
from agent.repair import can_attempt_repair
from agent.validation import ValidationRecord


def _record(**overrides: object) -> ValidationRecord:
    payload = {
        "task_id": "task-1",
        "command": "pytest tests",
        "argv": ("pytest", "tests"),
        "exit_code": 1,
        "stdout": "",
        "stderr": "AssertionError: expected 1",
        "duration_ms": 10,
        "timed_out": False,
        "denied": False,
    }
    payload.update(overrides)
    return ValidationRecord(**payload)  # type: ignore[arg-type]


def test_repairable_classification() -> None:
    assert classify_validation(_record()) is FailureClass.AGENT_REPAIRABLE
    assert (
        classify_output(stdout="", stderr="FAILED tests/test_x.py", binary="pytest", exit_code=1)
        is FailureClass.AGENT_REPAIRABLE
    )


def test_environment_classification() -> None:
    record = _record(stderr="Could not resolve host: pypi.org", argv=("python", "-m", "pip"))
    assert classify_validation(record) is FailureClass.ENVIRONMENT_FAILURE
    timeout = _record(timed_out=True, exit_code=None, stderr="validation timed out after 1s")
    assert classify_validation(timeout) is FailureClass.ENVIRONMENT_FAILURE


def test_escalation_classification() -> None:
    denied = _record(denied=True, deny_reason="forbidden command: terraform", exit_code=None)
    assert classify_validation(denied) is FailureClass.ESCALATION_REQUIRED
    unknown = classify_output(stdout="mystery", stderr="", binary="unknown-tool", exit_code=3)
    assert unknown is FailureClass.ESCALATION_REQUIRED


def test_retry_increment_and_limit() -> None:
    assert can_attempt_repair(3, 0) is True
    assert can_attempt_repair(3, 2) is True
    assert can_attempt_repair(3, 3) is False
    assert can_attempt_repair(1, 1) is False
