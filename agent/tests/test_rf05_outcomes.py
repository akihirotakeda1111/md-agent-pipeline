"""RF-05: typed outcomes, report schema, and fail-closed WorkUnit artifacts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from agent.classify import FailureClass
from agent.cli import (
    CYCLE_EXIT_CODES,
    DELIVERY_EXIT_CODES,
    EXIT_ENVIRONMENT,
    EXIT_INTERNAL,
    EXIT_INVALID,
    EXIT_OK,
    EXIT_POLICY,
    REVIEW_EXIT_CODES,
    WORK_UNIT_EXIT_CODES,
    _exit_for_error,
)
from agent.cycle import (
    REPAIR_ATTEMPT_LIMIT,
    CycleOutcome,
    CycleResult,
    validate_cycle_result,
)
from agent.delivery import (
    DeliveryOutcome,
    DeliveryResult,
    assert_commit_allowed,
    assert_pr_allowed,
    validate_delivery_result,
)
from agent.errors import AgentError
from agent.review_loop import (
    STICKY_LABEL_OUTCOMES,
    STICKY_REVIEW_ESCALATED,
    STICKY_REVIEW_FAILED,
    ReviewResult,
    _sticky_terminal_result,
    validate_review_result,
)
from agent.review_terminal import CodeRabbitTerminal, CodeRabbitTerminalKind
from agent.review_track import empty_review_track
from agent.review_types import ReviewOutcome
from agent.spec import bind_spec_identity, parse_spec
from agent.state import new_execution_state
from agent.tests.test_phase6 import EXAMPLE_SPEC, _report
from agent.workunit import (
    WORK_UNIT_REPORT_SCHEMA_VERSION,
    WorkUnitOutcome,
    derived_compat_booleans,
    load_work_unit_report,
    load_work_unit_report_schema,
    report_from_reconcile,
    validate_work_unit_report,
    work_unit_outcome_from_cycle,
    write_work_unit_report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spec():
    return bind_spec_identity(
        parse_spec(EXAMPLE_SPEC), repo_root=REPO_ROOT, spec_directory="specs/tasks"
    )


def _cycle_result(**overrides: object) -> CycleResult:
    spec = _spec()
    payload = {
        "outcome": CycleOutcome.TASK_COMPLETED,
        "spec_id": spec.id,
        "task_id": "task-1",
        "base_sha": "a" * 40,
        "state": new_execution_state(spec),
        "message": "validation passed",
    }
    payload.update(overrides)
    return CycleResult(**payload)  # type: ignore[arg-type]


def _terminal(kind: CodeRabbitTerminalKind) -> CodeRabbitTerminal:
    return CodeRabbitTerminal(
        kind=kind,
        source="check",
        head_sha="c" * 40,
        conclusion="",
        observed_at="",
    )


def test_result_types_use_boundary_enums() -> None:
    cycle = _cycle_result()
    assert isinstance(cycle.outcome, CycleOutcome)
    report = _report()
    assert isinstance(report.outcome, WorkUnitOutcome)
    delivery = DeliveryResult(
        outcome=DeliveryOutcome.PR_CREATED,
        pr_url="https://example.test/pull/1",
        pr_number=1,
        commit_sha="b" * 40,
        notice=None,
        summary="",
        message="created pull request",
    )
    assert isinstance(delivery.outcome, DeliveryOutcome)
    review = ReviewResult(
        outcome=ReviewOutcome.IN_REVIEW,
        spec_id="review-demo",
        pull_number=1,
        message="waiting",
    )
    assert isinstance(review.outcome, ReviewOutcome)


def test_enum_values_match_existing_strings_and_add_no_new_outcomes() -> None:
    assert [item.value for item in CycleOutcome] == [
        "TASK_COMPLETED",
        "FINAL_VERIFICATION_PASSED",
        "FAILED",
        "ESCALATED",
        "SCOPE_VIOLATION",
    ]
    assert [item.value for item in WorkUnitOutcome] == [
        "FINAL_VERIFICATION_PASSED",
        "FAILED",
        "ESCALATED",
        "SCOPE_VIOLATION",
        "INVALID_SPEC",
        "COMPLETED",
    ]
    assert [item.value for item in DeliveryOutcome] == ["PR_CREATED", "FAILED", "ESCALATED"]
    assert [item.value for item in ReviewOutcome] == [
        "IN_REVIEW",
        "REVIEW_FIX_PUSHED",
        "READY_FOR_HUMAN",
        "FAILED",
        "ESCALATED",
    ]


def test_unknown_and_cross_boundary_outcomes_are_rejected() -> None:
    with pytest.raises(AgentError) as exc_info:
        _cycle_result(outcome="NOT_A_REAL_OUTCOME")
    assert exc_info.value.code == "INVALID_CYCLE_RESULT"
    with pytest.raises(AgentError) as exc_info:
        _cycle_result(outcome=WorkUnitOutcome.COMPLETED)
    assert exc_info.value.code == "INVALID_CYCLE_RESULT"
    with pytest.raises(AgentError) as exc_info:
        _report(outcome="TASK_COMPLETED")
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"
    with pytest.raises(AgentError) as exc_info:
        DeliveryResult(
            outcome=CycleOutcome.FAILED,
            pr_url=None,
            pr_number=None,
            commit_sha=None,
            notice=None,
            summary="",
            message="nope",
            failure_class=FailureClass.ENVIRONMENT_FAILURE,
        )
    assert exc_info.value.code == "INVALID_DELIVERY_RESULT"


def test_success_outcome_rejects_failure_class_and_code() -> None:
    with pytest.raises(AgentError):
        _cycle_result(failure_class=FailureClass.ENVIRONMENT_FAILURE)
    with pytest.raises(AgentError):
        _cycle_result(code="NOPE")
    with pytest.raises(AgentError):
        _report(failure_class=FailureClass.ENVIRONMENT_FAILURE)
    with pytest.raises(AgentError):
        _report(code="NOPE")


def test_failed_and_escalated_invariants() -> None:
    with pytest.raises(AgentError):
        _cycle_result(
            outcome=CycleOutcome.FAILED,
            failure_class=FailureClass.AGENT_REPAIRABLE,
            message="environment failure is not sent to repair",
        )
    with pytest.raises(AgentError):
        _cycle_result(
            outcome=CycleOutcome.ESCALATED,
            failure_class=FailureClass.ENVIRONMENT_FAILURE,
            message="validation failure requires escalation",
        )
    with pytest.raises(AgentError):
        _cycle_result(
            outcome=CycleOutcome.SCOPE_VIOLATION,
            failure_class=FailureClass.AGENT_REPAIRABLE,
            message="SCOPE_VIOLATION: specs/x.md",
        )
    ok = _cycle_result(
        outcome=CycleOutcome.ESCALATED,
        failure_class=FailureClass.AGENT_REPAIRABLE,
        code=REPAIR_ATTEMPT_LIMIT,
        message="repair_attempt_limit reached",
    )
    assert ok.code == REPAIR_ATTEMPT_LIMIT
    assert ok.failure_class is FailureClass.AGENT_REPAIRABLE
    assert ok.message == "repair_attempt_limit reached"
    with pytest.raises(AgentError):
        _cycle_result(
            outcome=CycleOutcome.FAILED,
            failure_class=FailureClass.ENVIRONMENT_FAILURE,
            code="",
            message="environment failure is not sent to repair",
        )


def test_work_unit_compat_booleans_match_outcome() -> None:
    assert derived_compat_booleans(WorkUnitOutcome.FINAL_VERIFICATION_PASSED) == (True, True, True)
    assert derived_compat_booleans(WorkUnitOutcome.SCOPE_VIOLATION) == (False, False, False)
    for outcome in (
        WorkUnitOutcome.FAILED,
        WorkUnitOutcome.ESCALATED,
        WorkUnitOutcome.INVALID_SPEC,
        WorkUnitOutcome.COMPLETED,
    ):
        assert derived_compat_booleans(outcome) == (False, False, True)
    with pytest.raises(AgentError):
        _report(outcome="FINAL_VERIFICATION_PASSED", validation_passed=False)
    with pytest.raises(AgentError):
        _report(outcome="FINAL_VERIFICATION_PASSED", scope_allowed=False)
    with pytest.raises(AgentError):
        _report(outcome="SCOPE_VIOLATION", scope_allowed=True)
    with pytest.raises(AgentError):
        _report(outcome="FAILED", final_verification_passed=True)


def test_work_unit_report_state_fields_must_agree() -> None:
    spec = _spec()
    empty = new_execution_state(spec)
    with pytest.raises(AgentError) as exc_info:
        _report(completed_tasks=("task-1",), state=empty)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"
    with pytest.raises(AgentError) as exc_info:
        _report(repair_attempts=2, state=empty)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"
    with pytest.raises(AgentError) as exc_info:
        _report(branch="feature/other", state=empty)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"


def test_work_unit_report_rejects_unknown_duplicate_and_missing_tasks() -> None:
    spec = _spec()
    with pytest.raises(AgentError) as exc_info:
        _report(
            outcome="FAILED",
            completed_tasks=("task-1", "task-1"),
            state=replace(new_execution_state(spec), completed_tasks=("task-1", "task-1")),
        )
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"

    unknown = _report(
        outcome="FAILED",
        completed_tasks=("ghost",),
        state=replace(new_execution_state(spec), completed_tasks=("ghost",)),
    )
    with pytest.raises(AgentError) as exc_info:
        validate_work_unit_report(unknown, spec=spec)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"

    partial = _report(
        completed_tasks=("task-1",),
        state=replace(new_execution_state(spec), completed_tasks=("task-1",)),
    )
    with pytest.raises(AgentError) as exc_info:
        validate_work_unit_report(partial, spec=spec)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"
    validate_work_unit_report(_report(), spec=spec)


def test_load_work_unit_report_types_io_errors(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    with pytest.raises(AgentError) as exc_info:
        load_work_unit_report(missing)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"

    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / "report.json").mkdir()
    with pytest.raises(AgentError) as exc_info:
        load_work_unit_report(unreadable)
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"


def test_missing_report_fails_closed_before_delivery(tmp_path: Path) -> None:
    from agent.delivery import run_delivery

    with pytest.raises(AgentError) as exc_info:
        run_delivery(
            _spec(),
            repo_root=REPO_ROOT,
            report_dir=tmp_path / "absent",
            github=object(),  # type: ignore[arg-type]
        )
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"


def test_delivery_failure_code_is_not_failure_class() -> None:
    from agent.config import load_config
    from agent.delivery import _report_failure

    class Github:
        def list_open_pulls(self, head_branch=None):
            return []

        def create_issue(self, **kwargs):
            return {"number": 1}

    spec = _spec()
    github = Github()
    failed = _report_failure(spec, _report(outcome="FAILED"), load_config(), github)
    assert failed.code is None
    assert failed.failure_class is FailureClass.ENVIRONMENT_FAILURE
    coded = _report_failure(
        spec,
        _report(
            outcome="ESCALATED",
            failure_class=FailureClass.AGENT_REPAIRABLE,
            code="REPAIR_ATTEMPT_LIMIT",
        ),
        load_config(),
        github,
    )
    assert coded.code == "REPAIR_ATTEMPT_LIMIT"
    assert coded.failure_class is FailureClass.ESCALATION_REQUIRED


def test_unreadable_report_exit_is_not_internal() -> None:
    error = AgentError.invalid_input(
        "work unit report could not be read",
        code="INVALID_WORK_UNIT_REPORT",
    )
    assert _exit_for_error(error) == EXIT_INVALID


def test_reconcile_outcomes_remain_valid() -> None:
    spec = _spec()
    from agent.reconcile import ReconcileResult
    from agent.state import ExecutionStatus

    for status, outcome in (
        (ExecutionStatus.INVALID_SPEC, WorkUnitOutcome.INVALID_SPEC),
        (ExecutionStatus.COMPLETED, WorkUnitOutcome.COMPLETED),
    ):
        state = replace(new_execution_state(spec), state=status)
        report = report_from_reconcile(
            spec, "a" * 40, ReconcileResult("block", state, f"{status.value} is not auto-retried")
        )
        assert report.outcome is outcome
        assert report.skip_reason == f"{status.value} is not auto-retried"
        assert report.final_verification_passed is False


def test_cycle_to_work_unit_mapping_is_explicit() -> None:
    assert (
        work_unit_outcome_from_cycle(CycleOutcome.FINAL_VERIFICATION_PASSED)
        is WorkUnitOutcome.FINAL_VERIFICATION_PASSED
    )
    assert work_unit_outcome_from_cycle(CycleOutcome.FAILED) is WorkUnitOutcome.FAILED
    assert work_unit_outcome_from_cycle(CycleOutcome.ESCALATED) is WorkUnitOutcome.ESCALATED
    assert (
        work_unit_outcome_from_cycle(CycleOutcome.SCOPE_VIOLATION)
        is WorkUnitOutcome.SCOPE_VIOLATION
    )
    with pytest.raises(AgentError) as exc_info:
        work_unit_outcome_from_cycle(CycleOutcome.TASK_COMPLETED)
    assert exc_info.value.code == "UNSUPPORTED_CYCLE_OUTCOME"


def _valid_report_payload() -> dict:
    report = _report(patch_sha256="a" * 64)
    return report.to_json_dict()


def _write_payload(tmp_path: Path, payload: dict) -> Path:
    directory = tmp_path / "report"
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (directory / "changes.patch").write_text("", encoding="utf-8")
    return directory


def test_report_schema_rejects_invalid_artifacts(tmp_path: Path) -> None:
    valid = _valid_report_payload()
    load_work_unit_report(_write_payload(tmp_path / "ok", valid))

    missing_version = dict(valid)
    missing_version.pop("schema_version")
    with pytest.raises(AgentError) as exc_info:
        load_work_unit_report(_write_payload(tmp_path / "no-version", missing_version))
    assert exc_info.value.code == "INVALID_WORK_UNIT_REPORT"

    bad_version = dict(valid)
    bad_version["schema_version"] = 2
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "bad-version", bad_version))

    missing_field = dict(valid)
    missing_field.pop("spec_id")
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "missing", missing_field))

    extra = dict(valid)
    extra["unexpected"] = True
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "extra", extra))

    unknown_outcome = dict(valid)
    unknown_outcome["outcome"] = "TASK_COMPLETED"
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "task-completed", unknown_outcome))

    unknown_class = dict(valid)
    unknown_class["classification"] = "UNKNOWN"
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "class", unknown_class))

    bool_string = dict(valid)
    bool_string["final_verification_passed"] = "false"
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "bool", bool_string))

    int_string = dict(valid)
    int_string["repair_attempts"] = "2"
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "int", int_string))

    string_number = dict(valid)
    string_number["message"] = 1
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "str", string_number))

    bad_sha = dict(valid)
    bad_sha["spec_sha256"] = "not-a-sha"
    with pytest.raises(AgentError):
        load_work_unit_report(_write_payload(tmp_path / "sha", bad_sha))


def test_report_schema_does_not_duplicate_execution_state() -> None:
    schema = load_work_unit_report_schema()
    state = schema["properties"]["state"]
    assert state == {"type": "object"}
    dumped = json.dumps(schema)
    assert "FINAL_VALIDATING" not in dumped
    assert "schemaVersion" not in dumped


def test_load_work_unit_report_validates_execution_state(tmp_path: Path) -> None:
    payload = _valid_report_payload()
    payload["state"] = {"schemaVersion": 1}
    with pytest.raises(AgentError) as exc_info:
        load_work_unit_report(_write_payload(tmp_path, payload))
    assert "invalid execution state" in str(exc_info.value)


def test_skip_reason_round_trip(tmp_path: Path) -> None:
    report = _report(outcome="COMPLETED", skip_reason="COMPLETED is not auto-retried")
    directory = tmp_path / "report"
    write_work_unit_report(directory, report)
    loaded = load_work_unit_report(directory)
    assert loaded.skip_reason == "COMPLETED is not auto-retried"
    assert loaded.outcome is WorkUnitOutcome.COMPLETED


def test_schema_version_is_written(tmp_path: Path) -> None:
    directory = tmp_path / "report"
    write_work_unit_report(directory, _report(patch_sha256="b" * 64))
    payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == WORK_UNIT_REPORT_SCHEMA_VERSION
    assert payload["classification"] is None
    assert "failure_class" not in payload


def test_delivery_gate_uses_work_unit_outcome_not_booleans() -> None:
    report = _report()
    assert_commit_allowed(report)
    assert_pr_allowed(report)
    failed = _report(outcome="FAILED")
    with pytest.raises(AgentError) as exc_info:
        assert_commit_allowed(failed)
    assert exc_info.value.code == "COMMIT_BEFORE_VALIDATION"


def test_cli_maps_every_enum_member() -> None:
    assert set(CYCLE_EXIT_CODES) == set(CycleOutcome)
    assert set(WORK_UNIT_EXIT_CODES) == set(WorkUnitOutcome)
    assert set(DELIVERY_EXIT_CODES) == set(DeliveryOutcome)
    assert set(REVIEW_EXIT_CODES) == set(ReviewOutcome)
    assert CYCLE_EXIT_CODES[CycleOutcome.TASK_COMPLETED] == EXIT_OK
    assert CYCLE_EXIT_CODES[CycleOutcome.FINAL_VERIFICATION_PASSED] == EXIT_OK
    assert CYCLE_EXIT_CODES[CycleOutcome.SCOPE_VIOLATION] == EXIT_POLICY
    assert CYCLE_EXIT_CODES[CycleOutcome.FAILED] == EXIT_INVALID
    assert CYCLE_EXIT_CODES[CycleOutcome.ESCALATED] == EXIT_INVALID
    assert WORK_UNIT_EXIT_CODES[WorkUnitOutcome.FINAL_VERIFICATION_PASSED] == EXIT_OK
    assert WORK_UNIT_EXIT_CODES[WorkUnitOutcome.FAILED] == EXIT_ENVIRONMENT
    assert WORK_UNIT_EXIT_CODES[WorkUnitOutcome.SCOPE_VIOLATION] == EXIT_POLICY
    assert DELIVERY_EXIT_CODES[DeliveryOutcome.PR_CREATED] == EXIT_OK
    assert DELIVERY_EXIT_CODES[DeliveryOutcome.FAILED] == EXIT_ENVIRONMENT
    assert DELIVERY_EXIT_CODES[DeliveryOutcome.ESCALATED] == EXIT_POLICY
    assert REVIEW_EXIT_CODES[ReviewOutcome.IN_REVIEW] == EXIT_OK
    assert REVIEW_EXIT_CODES[ReviewOutcome.REVIEW_FIX_PUSHED] == EXIT_OK
    assert REVIEW_EXIT_CODES[ReviewOutcome.READY_FOR_HUMAN] == EXIT_OK
    assert REVIEW_EXIT_CODES[ReviewOutcome.FAILED] == EXIT_ENVIRONMENT
    assert REVIEW_EXIT_CODES[ReviewOutcome.ESCALATED] == EXIT_POLICY


def test_unknown_python_exception_is_internal() -> None:
    assert _exit_for_error(RuntimeError("boom")) == EXIT_INTERNAL


def test_sticky_label_mapping_uses_review_outcome() -> None:
    assert STICKY_LABEL_OUTCOMES["agent:ready"] is ReviewOutcome.READY_FOR_HUMAN
    assert STICKY_LABEL_OUTCOMES["agent:escalated"] is ReviewOutcome.ESCALATED
    assert STICKY_LABEL_OUTCOMES["agent:failed"] is ReviewOutcome.FAILED


class _StickyGithub:
    def __init__(self, label: str) -> None:
        self._label = label

    def list_issue_labels(self, _number: int) -> list[dict[str, str]]:
        return [{"name": self._label}]


def test_sticky_failed_and_escalated_fill_generic_codes() -> None:
    spec = _spec()
    track = replace(empty_review_track(spec), head_sha="c" * 40)
    failed = _sticky_terminal_result(
        _StickyGithub("agent:failed"),  # type: ignore[arg-type]
        spec,
        7,
        track,
        "c" * 40,
        _terminal(CodeRabbitTerminalKind.NONE),
    )
    assert failed is not None
    assert failed.outcome is ReviewOutcome.FAILED
    assert failed.failure_class is FailureClass.ENVIRONMENT_FAILURE
    assert failed.code == STICKY_REVIEW_FAILED

    none_terminal = _terminal(CodeRabbitTerminalKind.NONE)
    escalated = _sticky_terminal_result(
        _StickyGithub("agent:escalated"),  # type: ignore[arg-type]
        spec,
        7,
        track,
        "c" * 40,
        none_terminal,
    )
    assert escalated is not None
    assert escalated.outcome is ReviewOutcome.ESCALATED
    assert escalated.failure_class is FailureClass.ESCALATION_REQUIRED
    assert escalated.code == STICKY_REVIEW_ESCALATED

    skipped = _terminal(CodeRabbitTerminalKind.SKIPPED)
    sticky_skipped = _sticky_terminal_result(
        _StickyGithub("agent:escalated"),  # type: ignore[arg-type]
        spec,
        7,
        track,
        "c" * 40,
        skipped,
    )
    assert sticky_skipped is not None
    assert sticky_skipped.code == skipped.escalation_code()


def test_json_serialization_keeps_existing_strings() -> None:
    cycle = _cycle_result(outcome=CycleOutcome.FINAL_VERIFICATION_PASSED, task_id=None)
    assert cycle.to_json_dict()["outcome"] == "FINAL_VERIFICATION_PASSED"
    report = _report()
    payload = report.to_json_dict()
    assert payload["outcome"] == "FINAL_VERIFICATION_PASSED"
    assert "classification" in payload
    delivery = DeliveryResult(
        outcome=DeliveryOutcome.PR_CREATED,
        pr_url=None,
        pr_number=1,
        commit_sha=None,
        notice=None,
        summary="",
        message="created pull request",
    )
    assert delivery.to_json_dict()["outcome"] == "PR_CREATED"
    review = ReviewResult(
        outcome=ReviewOutcome.READY_FOR_HUMAN,
        spec_id="x",
        pull_number=1,
        message="ready",
    )
    assert review.to_json_dict()["outcome"] == "READY_FOR_HUMAN"


def test_validators_are_exported() -> None:
    validate_cycle_result(_cycle_result())
    validate_work_unit_report(_report())
    validate_delivery_result(
        DeliveryResult(
            outcome=DeliveryOutcome.PR_CREATED,
            pr_url=None,
            pr_number=1,
            commit_sha=None,
            notice=None,
            summary="",
            message="created pull request",
        )
    )
    validate_review_result(
        ReviewResult(
            outcome=ReviewOutcome.IN_REVIEW,
            spec_id="x",
            pull_number=1,
            message="waiting",
        )
    )
