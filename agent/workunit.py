"""Run the full work unit (all tasks + final verification) without Git writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.config import AgentConfig, load_config
from agent.cycle import CycleResult, run_task_cycle
from agent.errors import AgentError
from agent.events import (
    FAILED,
    FINAL_VALIDATION_PASSED,
    SPEC_DISCOVERED,
    SPEC_VALIDATED,
    STATE_INITIALIZED,
    TASK_COMPLETED,
    WORKFLOW_COMPLETED,
    emit,
)
from agent.github_api import GitHubClient
from agent.gitutil import capture_snapshot, change_path_list, collect_changes
from agent.gitwrite import export_patch, head_sha
from agent.reconcile import ReconcileResult, load_state_or_new, reconcile_work_unit
from agent.spec import TaskSpec, parse_spec
from agent.state import ExecutionState, new_execution_state


def file_sha256(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass
class WorkUnitReport:
    outcome: str
    spec_id: str
    spec_path: str
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
    classification: str | None = None
    current_task: str | None = None
    skip_reason: str | None = None
    patch_file: str = "changes.patch"
    patch_sha256: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "spec_id": self.spec_id,
            "spec_path": self.spec_path,
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
            "classification": self.classification,
            "current_task": self.current_task,
            "skip_reason": self.skip_reason,
            "patch_file": self.patch_file,
            "patch_sha256": self.patch_sha256,
        }


def write_work_unit_report(report_dir: Path | str, report: WorkUnitReport) -> Path:
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "report.json"
    path.write_text(
        json.dumps(report.to_json_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_work_unit_report(report_dir: Path | str) -> WorkUnitReport:
    path = Path(report_dir) / "report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    from agent.state import state_from_dict

    return WorkUnitReport(
        outcome=str(payload["outcome"]),
        spec_id=str(payload["spec_id"]),
        spec_path=str(payload["spec_path"]),
        base_sha=str(payload["base_sha"]),
        branch=str(payload["branch"]),
        state=state_from_dict(payload["state"]),
        completed_tasks=tuple(payload.get("completed_tasks") or ()),
        changed_files=tuple(payload.get("changed_files") or ()),
        validation_results=tuple(payload.get("validation_results") or ()),
        repair_attempts=int(payload.get("repair_attempts") or 0),
        final_verification_passed=bool(payload.get("final_verification_passed")),
        validation_passed=bool(payload.get("validation_passed")),
        scope_allowed=bool(payload.get("scope_allowed", True)),
        message=str(payload.get("message") or ""),
        classification=payload.get("classification"),
        current_task=payload.get("current_task"),
        skip_reason=payload.get("skip_reason"),
        patch_file=str(payload.get("patch_file") or "changes.patch"),
        patch_sha256=str(payload.get("patch_sha256") or ""),
    )


def run_work_unit(
    spec: TaskSpec | Path | str,
    *,
    repo_root: Path | str,
    report_dir: Path | str,
    config: AgentConfig | None = None,
    env: Mapping[str, str] | None = None,
    executor: Any | None = None,
    github: GitHubClient | None = None,
    persist_state: bool = False,
) -> WorkUnitReport:
    cfg = config or load_config()
    root = Path(repo_root)
    parsed = spec if isinstance(spec, TaskSpec) else parse_spec(spec)
    emit(SPEC_DISCOVERED, "task spec discovered", task_id=parsed.id, state="PENDING")
    emit(SPEC_VALIDATED, "task spec is valid", task_id=parsed.id, state="PENDING")
    try:
        reconciled = reconcile_work_unit(parsed, root, github=github, persist_state=persist_state)
    except AgentError as exc:
        snapshot = capture_snapshot(root)
        try:
            state = load_state_or_new(parsed, root)
        except Exception:
            state = new_execution_state(parsed)
        escalate_codes = {
            "STATE_GIT_MISMATCH",
            "STATE_BRANCH_MISMATCH",
            "STATE_PR_MISMATCH",
            "STATE_COMMIT_MISMATCH",
            "UNSAFE_RECONCILE",
        }
        report = WorkUnitReport(
            outcome="ESCALATED" if exc.code in escalate_codes else "FAILED",
            spec_id=parsed.id,
            spec_path=parsed.source_path or "",
            base_sha=snapshot.base_sha,
            branch=parsed.target_branch,
            state=state,
            completed_tasks=state.completed_tasks,
            changed_files=(),
            validation_results=(),
            repair_attempts=state.repair_attempts,
            final_verification_passed=False,
            validation_passed=False,
            scope_allowed=True,
            message=str(exc),
            current_task=state.current_task,
            skip_reason=str(exc),
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
    if reconciled.already_delivered:
        report = _report_from_reconcile(parsed, snapshot.base_sha, reconciled, "ALREADY_DELIVERED")
        _export_and_write(root, snapshot.base_sha, report_dir, report)
        emit(
            WORKFLOW_COMPLETED,
            "existing pull request reused",
            task_id=parsed.id,
            state=report.state.state.value,
        )
        return report
    if reconciled.action == "block":
        report = _report_from_reconcile(
            parsed, snapshot.base_sha, reconciled, reconciled.state.state.value
        )
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
        if last.outcome == "TASK_COMPLETED":
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
    if last.outcome == "FINAL_VERIFICATION_PASSED":
        emit(
            FINAL_VALIDATION_PASSED,
            "final verification passed",
            task_id=parsed.id,
            state=last.state.state.value,
        )
    elif last.outcome in {"FAILED", "ESCALATED", "SCOPE_VIOLATION"}:
        event = FAILED if last.outcome == "FAILED" else last.outcome
        emit(event, last.message, task_id=parsed.id, state=last.state.state.value)

    base_sha = last.base_sha or snapshot.base_sha or head_sha(root)
    changed = change_path_list(collect_changes(root, base_sha))
    report = WorkUnitReport(
        outcome=last.outcome,
        spec_id=parsed.id,
        spec_path=parsed.source_path or "",
        base_sha=base_sha,
        branch=parsed.target_branch,
        state=last.state,
        completed_tasks=last.state.completed_tasks,
        changed_files=changed,
        validation_results=tuple(validations),
        repair_attempts=last.state.repair_attempts,
        final_verification_passed=last.outcome == "FINAL_VERIFICATION_PASSED",
        validation_passed=last.outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"},
        scope_allowed=last.scope.allowed
        if last.scope is not None
        else last.outcome != "SCOPE_VIOLATION",
        message=last.message,
        classification=None if last.classification is None else last.classification.value,
        current_task=last.task_id or last.state.current_task,
    )
    _export_and_write(root, report.base_sha, report_dir, report)
    emit(
        WORKFLOW_COMPLETED,
        report.message or report.outcome,
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


def _report_from_reconcile(
    spec: TaskSpec, base_sha: str, reconciled: ReconcileResult, outcome: str
) -> WorkUnitReport:
    return WorkUnitReport(
        outcome=outcome,
        spec_id=spec.id,
        spec_path=spec.source_path or "",
        base_sha=base_sha,
        branch=spec.target_branch,
        state=reconciled.state,
        completed_tasks=reconciled.state.completed_tasks,
        changed_files=(),
        validation_results=(),
        repair_attempts=reconciled.state.repair_attempts,
        final_verification_passed=reconciled.already_delivered,
        validation_passed=reconciled.already_delivered,
        scope_allowed=True,
        message=reconciled.reason,
        current_task=reconciled.state.current_task,
        skip_reason=reconciled.reason,
    )
