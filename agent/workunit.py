"""Run the full work unit (all tasks + final verification) without Git writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from agent.classify import FailureClass
from agent.config import AgentConfig, load_config
from agent.cycle import CycleOutcome, CycleResult, run_task_cycle
from agent.errors import AgentError
from agent.events import (
    FAILED,
    FINAL_VALIDATION_PASSED,
    SCOPE_VIOLATION,
    SPEC_DISCOVERED,
    SPEC_VALIDATED,
    STATE_INITIALIZED,
    TASK_COMPLETED,
    WORKFLOW_COMPLETED,
    emit,
)
from agent.gitutil import capture_snapshot, change_path_list, collect_changes
from agent.gitwrite import export_patch, head_sha
from agent.reconcile import ReconcileResult, load_state_or_new, prepare_execution_state
from agent.scope import validate_spec_scope_policy
from agent.spec import TaskSpec, bind_spec_identity, parse_spec
from agent.state import ExecutionState, current_state_relpath, new_execution_state, state_from_dict

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "work-unit-report.schema.json"
WORK_UNIT_REPORT_SCHEMA_VERSION = 1
_WORK_UNIT_REPORT_SCHEMA: dict[str, Any] | None = None
_ESCALATED_FAILURE_CLASSES = frozenset(
    {
        FailureClass.AGENT_REPAIRABLE,
        FailureClass.ESCALATION_REQUIRED,
    }
)


class WorkUnitOutcome(StrEnum):
    FINAL_VERIFICATION_PASSED = "FINAL_VERIFICATION_PASSED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    INVALID_SPEC = "INVALID_SPEC"
    COMPLETED = "COMPLETED"


_RECONCILE_OUTCOMES = frozenset(
    {
        WorkUnitOutcome.INVALID_SPEC,
        WorkUnitOutcome.COMPLETED,
    }
)
_CYCLE_TO_WORK_UNIT = {
    CycleOutcome.FINAL_VERIFICATION_PASSED: WorkUnitOutcome.FINAL_VERIFICATION_PASSED,
    CycleOutcome.FAILED: WorkUnitOutcome.FAILED,
    CycleOutcome.ESCALATED: WorkUnitOutcome.ESCALATED,
    CycleOutcome.SCOPE_VIOLATION: WorkUnitOutcome.SCOPE_VIOLATION,
}


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_work_unit_report_schema() -> dict[str, Any]:
    global _WORK_UNIT_REPORT_SCHEMA
    if _WORK_UNIT_REPORT_SCHEMA is None:
        _WORK_UNIT_REPORT_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _WORK_UNIT_REPORT_SCHEMA


def validate_work_unit_report_dict(instance: dict[str, Any]) -> None:
    try:
        jsonschema.validate(instance=instance, schema=load_work_unit_report_schema())
    except JsonSchemaValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "(root)"
        raise AgentError.invalid_input(
            f"invalid work unit report: {path}: {exc.message}",
            code="INVALID_WORK_UNIT_REPORT",
        ) from exc


def _as_work_unit_outcome(value: object) -> WorkUnitOutcome:
    if isinstance(value, WorkUnitOutcome):
        return value
    if isinstance(value, StrEnum):
        raise AgentError.invalid_input(
            f"invalid work unit outcome: {value!r}",
            code="INVALID_WORK_UNIT_REPORT",
        )
    if isinstance(value, str):
        try:
            return WorkUnitOutcome(value)
        except ValueError:
            pass
    raise AgentError.invalid_input(
        f"invalid work unit outcome: {value!r}",
        code="INVALID_WORK_UNIT_REPORT",
    )


def _as_failure_class(value: object) -> FailureClass | None:
    if value is None:
        return None
    if isinstance(value, FailureClass):
        return value
    if isinstance(value, StrEnum):
        raise AgentError.invalid_input(
            f"invalid work unit failure class: {value!r}",
            code="INVALID_WORK_UNIT_REPORT",
        )
    if isinstance(value, str):
        try:
            return FailureClass(value)
        except ValueError:
            pass
    raise AgentError.invalid_input(
        f"invalid work unit failure class: {value!r}",
        code="INVALID_WORK_UNIT_REPORT",
    )


def derived_compat_booleans(outcome: WorkUnitOutcome) -> tuple[bool, bool, bool]:
    if outcome is WorkUnitOutcome.FINAL_VERIFICATION_PASSED:
        return True, True, True
    if outcome is WorkUnitOutcome.SCOPE_VIOLATION:
        return False, False, False
    return False, False, True


def work_unit_outcome_from_cycle(outcome: CycleOutcome) -> WorkUnitOutcome:
    mapped = _CYCLE_TO_WORK_UNIT.get(outcome)
    if mapped is None:
        raise AgentError.internal_failure(
            f"cycle outcome {getattr(outcome, 'value', outcome)!r} cannot be a work unit report",
            code="UNSUPPORTED_CYCLE_OUTCOME",
        )
    return mapped


def _invalid_work_unit_report(message: str) -> AgentError:
    return AgentError.invalid_input(message, code="INVALID_WORK_UNIT_REPORT")


def validate_work_unit_report(report: WorkUnitReport, spec: TaskSpec | None = None) -> None:
    if not isinstance(report.outcome, WorkUnitOutcome):
        raise _invalid_work_unit_report(
            f"work unit outcome is not WorkUnitOutcome: {report.outcome!r}"
        )
    if report.schema_version != WORK_UNIT_REPORT_SCHEMA_VERSION:
        raise _invalid_work_unit_report(
            f"unsupported work unit report schema_version: {report.schema_version!r}"
        )
    if report.code is not None and (not isinstance(report.code, str) or report.code == ""):
        raise _invalid_work_unit_report("work unit report code must be a non-empty string when set")
    expected_fv, expected_validation, expected_scope = derived_compat_booleans(report.outcome)
    if report.final_verification_passed is not expected_fv:
        raise _invalid_work_unit_report("final_verification_passed does not match outcome")
    if report.validation_passed is not expected_validation:
        raise _invalid_work_unit_report("validation_passed does not match outcome")
    if report.scope_allowed is not expected_scope:
        raise _invalid_work_unit_report("scope_allowed does not match outcome")
    if tuple(report.completed_tasks) != tuple(report.state.completed_tasks):
        raise _invalid_work_unit_report("completed_tasks does not match execution state")
    if report.repair_attempts != report.state.repair_attempts:
        raise _invalid_work_unit_report("repair_attempts does not match execution state")
    if report.branch != report.state.branch:
        raise _invalid_work_unit_report("branch does not match execution state")
    if len(report.completed_tasks) != len(set(report.completed_tasks)):
        raise _invalid_work_unit_report("completed_tasks contains duplicate task ids")
    if spec is not None:
        spec_ids = tuple(task.id for task in spec.tasks)
        spec_set = set(spec_ids)
        unknown = [task_id for task_id in report.completed_tasks if task_id not in spec_set]
        if unknown:
            raise _invalid_work_unit_report(
                "completed_tasks contains unknown task ids: " + ", ".join(unknown)
            )
        if report.outcome is WorkUnitOutcome.FINAL_VERIFICATION_PASSED:
            missing = [
                task_id for task_id in spec_ids if task_id not in set(report.completed_tasks)
            ]
            if missing:
                raise _invalid_work_unit_report(
                    "FINAL_VERIFICATION_PASSED requires all tasks completed; missing: "
                    + ", ".join(missing)
                )
    if report.outcome in _RECONCILE_OUTCOMES:
        return
    if report.outcome is WorkUnitOutcome.FINAL_VERIFICATION_PASSED:
        if report.failure_class is not None:
            raise AgentError.invalid_input(
                "FINAL_VERIFICATION_PASSED must not set a failure class",
                code="INVALID_WORK_UNIT_REPORT",
            )
        if report.code is not None:
            raise AgentError.invalid_input(
                "FINAL_VERIFICATION_PASSED must not set a code",
                code="INVALID_WORK_UNIT_REPORT",
            )
        return
    if report.outcome is WorkUnitOutcome.FAILED:
        if report.failure_class is not FailureClass.ENVIRONMENT_FAILURE:
            raise AgentError.invalid_input(
                "FAILED work unit report requires ENVIRONMENT_FAILURE",
                code="INVALID_WORK_UNIT_REPORT",
            )
        return
    if report.outcome is WorkUnitOutcome.ESCALATED:
        if report.failure_class not in _ESCALATED_FAILURE_CLASSES:
            raise AgentError.invalid_input(
                "ESCALATED work unit report requires AGENT_REPAIRABLE or ESCALATION_REQUIRED",
                code="INVALID_WORK_UNIT_REPORT",
            )
        return
    if report.outcome is WorkUnitOutcome.SCOPE_VIOLATION:
        if report.failure_class is not FailureClass.ESCALATION_REQUIRED:
            raise AgentError.invalid_input(
                "SCOPE_VIOLATION work unit report requires ESCALATION_REQUIRED",
                code="INVALID_WORK_UNIT_REPORT",
            )
        return
    raise AgentError.invalid_input(
        f"unsupported work unit outcome: {report.outcome!r}",
        code="INVALID_WORK_UNIT_REPORT",
    )


@dataclass
class WorkUnitReport:
    outcome: WorkUnitOutcome
    spec_id: str
    spec_path: str
    spec_sha256: str
    base_sha: str
    branch: str
    state: ExecutionState
    completed_tasks: tuple[str, ...]
    changed_files: tuple[str, ...]
    validation_results: tuple[str, ...]
    repair_attempts: int
    final_verification_passed: bool
    validation_passed: bool
    scope_allowed: bool
    message: str
    failure_class: FailureClass | None = None
    code: str | None = None
    current_task: str | None = None
    skip_reason: str | None = None
    patch_file: str = "changes.patch"
    patch_sha256: str = ""
    schema_version: int = WORK_UNIT_REPORT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.outcome = _as_work_unit_outcome(self.outcome)
        self.failure_class = _as_failure_class(self.failure_class)
        validate_work_unit_report(self)

    def to_json_dict(self) -> dict[str, Any]:
        validate_work_unit_report(self)
        return {
            "schema_version": self.schema_version,
            "outcome": self.outcome.value,
            "spec_id": self.spec_id,
            "spec_path": self.spec_path,
            "spec_sha256": self.spec_sha256,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "state": self.state.to_json_dict(),
            "completed_tasks": list(self.completed_tasks),
            "changed_files": list(self.changed_files),
            "validation_results": list(self.validation_results),
            "repair_attempts": self.repair_attempts,
            "final_verification_passed": self.final_verification_passed,
            "validation_passed": self.validation_passed,
            "scope_allowed": self.scope_allowed,
            "message": self.message,
            "classification": None if self.failure_class is None else self.failure_class.value,
            "code": self.code,
            "current_task": self.current_task,
            "skip_reason": self.skip_reason,
            "patch_file": self.patch_file,
            "patch_sha256": self.patch_sha256,
        }


def _build_work_unit_report(
    *,
    outcome: WorkUnitOutcome,
    spec: TaskSpec,
    base_sha: str,
    state: ExecutionState,
    completed_tasks: tuple[str, ...],
    changed_files: tuple[str, ...],
    validation_results: tuple[str, ...],
    repair_attempts: int,
    message: str,
    failure_class: FailureClass | None = None,
    code: str | None = None,
    current_task: str | None = None,
    skip_reason: str | None = None,
    patch_file: str = "changes.patch",
    patch_sha256: str = "",
    branch: str | None = None,
    validate_spec_tasks: bool = True,
) -> WorkUnitReport:
    final_verification_passed, validation_passed, allowed = derived_compat_booleans(outcome)
    report = WorkUnitReport(
        outcome=outcome,
        spec_id=spec.id,
        spec_path=spec.source_path or "",
        spec_sha256=spec.spec_sha256,
        base_sha=base_sha,
        branch=spec.target_branch if branch is None else branch,
        state=state,
        completed_tasks=completed_tasks,
        changed_files=changed_files,
        validation_results=validation_results,
        repair_attempts=repair_attempts,
        final_verification_passed=final_verification_passed,
        validation_passed=validation_passed,
        scope_allowed=allowed,
        message=message,
        failure_class=failure_class,
        code=code,
        current_task=current_task,
        skip_reason=skip_reason,
        patch_file=patch_file,
        patch_sha256=patch_sha256,
    )
    validate_work_unit_report(report, spec=spec if validate_spec_tasks else None)
    return report


def report_from_cycle(
    spec: TaskSpec,
    last: CycleResult,
    *,
    base_sha: str,
    changed_files: tuple[str, ...],
    validation_results: tuple[str, ...],
) -> WorkUnitReport:
    outcome = work_unit_outcome_from_cycle(last.outcome)
    return _build_work_unit_report(
        outcome=outcome,
        spec=spec,
        base_sha=base_sha,
        state=last.state,
        completed_tasks=last.state.completed_tasks,
        changed_files=changed_files,
        validation_results=validation_results,
        repair_attempts=last.state.repair_attempts,
        message=last.message,
        failure_class=last.failure_class,
        code=last.code,
        current_task=last.task_id or last.state.current_task,
    )


def report_from_reconcile(
    spec: TaskSpec, base_sha: str, reconciled: ReconcileResult
) -> WorkUnitReport:
    outcome = _as_work_unit_outcome(reconciled.state.state.value)
    failure_class = None
    if outcome is WorkUnitOutcome.ESCALATED:
        failure_class = FailureClass.ESCALATION_REQUIRED
    elif outcome is WorkUnitOutcome.SCOPE_VIOLATION:
        failure_class = FailureClass.ESCALATION_REQUIRED
    elif outcome is WorkUnitOutcome.FAILED:
        failure_class = FailureClass.ENVIRONMENT_FAILURE
    return _build_work_unit_report(
        outcome=outcome,
        spec=spec,
        base_sha=base_sha,
        state=reconciled.state,
        completed_tasks=reconciled.state.completed_tasks,
        changed_files=(),
        validation_results=(),
        repair_attempts=reconciled.state.repair_attempts,
        message=reconciled.reason,
        failure_class=failure_class,
        current_task=reconciled.state.current_task,
        skip_reason=reconciled.reason,
    )


def report_from_preparation_error(
    spec: TaskSpec,
    *,
    base_sha: str,
    state: ExecutionState,
    error: AgentError,
    state_rel: str,
) -> WorkUnitReport:
    if error.code == "STATE_TAMPERED":
        # Caller supplies new_execution_state(spec); keep spec branch and spec-task checks.
        message = f"STATE_TAMPERED: {state_rel}"
        return _build_work_unit_report(
            outcome=WorkUnitOutcome.SCOPE_VIOLATION,
            spec=spec,
            base_sha=base_sha,
            state=state,
            completed_tasks=state.completed_tasks,
            changed_files=(),
            validation_results=(),
            repair_attempts=state.repair_attempts,
            message=message,
            failure_class=FailureClass.ESCALATION_REQUIRED,
            code="STATE_TAMPERED",
            current_task=None,
            skip_reason=message,
        )
    escalate_codes = {
        "STATE_BRANCH_MISMATCH",
        "UNSAFE_RECONCILE",
    }
    if error.code in escalate_codes:
        outcome = WorkUnitOutcome.ESCALATED
        failure_class = FailureClass.ESCALATION_REQUIRED
    else:
        outcome = WorkUnitOutcome.FAILED
        failure_class = FailureClass.ENVIRONMENT_FAILURE
    return _build_work_unit_report(
        outcome=outcome,
        spec=spec,
        base_sha=base_sha,
        state=state,
        completed_tasks=state.completed_tasks,
        changed_files=(),
        validation_results=(),
        repair_attempts=state.repair_attempts,
        message=str(error),
        failure_class=failure_class,
        code=error.code,
        current_task=state.current_task,
        skip_reason=str(error),
        branch=state.branch,
        validate_spec_tasks=False,
    )


def write_work_unit_report(report_dir: Path | str, report: WorkUnitReport) -> Path:
    validate_work_unit_report(report)
    payload = report.to_json_dict()
    validate_work_unit_report_dict(payload)
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_work_unit_report(
    report_dir: Path | str, *, spec: TaskSpec | None = None
) -> WorkUnitReport:
    path = Path(report_dir) / "report.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentError.invalid_input(
            f"work unit report could not be read: {path}",
            code="INVALID_WORK_UNIT_REPORT",
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentError.invalid_input(
            "work unit report is not valid JSON",
            code="INVALID_WORK_UNIT_REPORT",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentError.invalid_input(
            "work unit report must be a JSON object",
            code="INVALID_WORK_UNIT_REPORT",
        )
    validate_work_unit_report_dict(payload)
    state = state_from_dict(payload["state"])
    outcome = _as_work_unit_outcome(payload["outcome"])
    failure_class = _as_failure_class(payload["classification"])
    report = WorkUnitReport(
        outcome=outcome,
        spec_id=payload["spec_id"],
        spec_path=payload["spec_path"],
        spec_sha256=payload["spec_sha256"],
        base_sha=payload["base_sha"],
        branch=payload["branch"],
        state=state,
        completed_tasks=tuple(payload["completed_tasks"]),
        changed_files=tuple(payload["changed_files"]),
        validation_results=tuple(payload["validation_results"]),
        repair_attempts=payload["repair_attempts"],
        final_verification_passed=payload["final_verification_passed"],
        validation_passed=payload["validation_passed"],
        scope_allowed=payload["scope_allowed"],
        message=payload["message"],
        failure_class=failure_class,
        code=payload["code"],
        current_task=payload["current_task"],
        skip_reason=payload["skip_reason"],
        patch_file=payload["patch_file"],
        patch_sha256=payload["patch_sha256"],
        schema_version=payload["schema_version"],
    )
    validate_work_unit_report(report, spec=spec)
    return report


def run_work_unit(
    spec: TaskSpec | Path | str,
    *,
    repo_root: Path | str,
    report_dir: Path | str,
    config: AgentConfig | None = None,
    env: Mapping[str, str] | None = None,
    executor: Any | None = None,
    persist_state: bool = False,
) -> WorkUnitReport:
    cfg = config or load_config()
    root = Path(repo_root)
    parsed = spec if isinstance(spec, TaskSpec) else parse_spec(spec)
    validate_spec_scope_policy(parsed, cfg.runtime_edit_policy)
    parsed = bind_spec_identity(
        parsed,
        repo_root=root,
        spec_directory=cfg.task_spec.directory,
    )
    emit(SPEC_DISCOVERED, "task spec discovered", task_id=parsed.id, state="PENDING")
    emit(SPEC_VALIDATED, "task spec is valid", task_id=parsed.id, state="PENDING")
    try:
        reconciled = prepare_execution_state(parsed, root, persist_state=persist_state, config=cfg)
    except AgentError as exc:
        snapshot = capture_snapshot(root)
        if exc.code == "STATE_TAMPERED":
            report = report_from_preparation_error(
                parsed,
                base_sha=snapshot.base_sha,
                state=new_execution_state(parsed),
                error=exc,
                state_rel=current_state_relpath(parsed.id, cfg),
            )
            _export_and_write(root, snapshot.base_sha, report_dir, report)
            emit(SCOPE_VIOLATION, report.message, task_id=parsed.id, state=report.state.state.value)
            return report
        try:
            state = (
                load_state_or_new(parsed, root, config=cfg)
                if persist_state
                else new_execution_state(parsed)
            )
        except Exception:
            state = new_execution_state(parsed)
        report = report_from_preparation_error(
            parsed,
            base_sha=snapshot.base_sha,
            state=state,
            error=exc,
            state_rel=current_state_relpath(parsed.id, cfg),
        )
        _export_and_write(root, snapshot.base_sha, report_dir, report)
        return report
    emit(
        STATE_INITIALIZED,
        reconciled.reason,
        task_id=parsed.id,
        state=reconciled.state.state.value,
    )
    snapshot = capture_snapshot(root)
    if reconciled.action == "block":
        report = report_from_reconcile(parsed, snapshot.base_sha, reconciled)
        _export_and_write(root, snapshot.base_sha, report_dir, report)
        emit(
            WORKFLOW_COMPLETED, reconciled.reason, task_id=parsed.id, state=report.state.state.value
        )
        return report

    last: CycleResult | None = None
    validations: list[str] = []
    current_state = reconciled.state
    limit = len(parsed.tasks) + 2
    for _ in range(limit):
        last = run_task_cycle(
            parsed,
            repo_root=root,
            config=cfg,
            env=env,
            executor=executor,
            state=current_state,
            persist_state=persist_state,
        )
        current_state = last.state
        validations.extend(record.command for record in last.validations)
        if last.outcome is CycleOutcome.TASK_COMPLETED:
            emit(
                TASK_COMPLETED,
                last.message,
                task_id=parsed.id,
                state=last.state.state.value,
                extra={"spec_task": last.task_id},
            )
            continue
        break
    else:
        raise AgentError.escalation_required(
            "work unit exceeded task loop bound",
            code="UNSAFE_RECONCILE",
        )
    assert last is not None
    if last.outcome is CycleOutcome.FINAL_VERIFICATION_PASSED:
        emit(
            FINAL_VALIDATION_PASSED,
            "final verification passed",
            task_id=parsed.id,
            state=last.state.state.value,
        )
    elif last.outcome in {
        CycleOutcome.FAILED,
        CycleOutcome.ESCALATED,
        CycleOutcome.SCOPE_VIOLATION,
    }:
        event = FAILED if last.outcome is CycleOutcome.FAILED else last.outcome.value
        emit(event, last.message, task_id=parsed.id, state=last.state.state.value)

    base_sha = last.base_sha or snapshot.base_sha or head_sha(root)
    changed = change_path_list(collect_changes(root, base_sha))
    report = report_from_cycle(
        parsed,
        last,
        base_sha=base_sha,
        changed_files=tuple(changed),
        validation_results=tuple(validations),
    )
    _export_and_write(root, report.base_sha, report_dir, report)
    emit(
        WORKFLOW_COMPLETED,
        report.message or report.outcome.value,
        task_id=parsed.id,
        state=report.state.state.value,
    )
    return report


def _export_and_write(
    root: Path, base_sha: str, report_dir: Path | str, report: WorkUnitReport
) -> None:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    patch_path = directory / report.patch_file
    export_patch(root, base_sha, patch_path)
    report.patch_sha256 = file_sha256(patch_path)
    write_work_unit_report(directory, report)
