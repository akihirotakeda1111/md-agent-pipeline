"""Commit, push, and open a Pull Request after Final Verification.

Codex credentials must never be present in this process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent.classify import FailureClass, classify_validation
from agent.config import AgentConfig, load_config
from agent.cycle import run_final_verification
from agent.errors import AgentError
from agent.events import (
    DELIVERY_VALIDATION_PASSED,
    DELIVERY_VALIDATION_STARTED,
    ESCALATED,
    FAILED,
    PR_CREATED,
    WORKFLOW_COMPLETED,
    emit,
    emit_notification_failed_best_effort,
)
from agent.github_api import GitHubClient, github_client_from_env
from agent.gitutil import (
    assert_clean_for_delivery,
    change_path_list,
    collect_changes,
)
from agent.gitwrite import (
    apply_patch,
    checkout_delivery_parent,
    commit_paths,
    head_sha,
    push_branch,
)
from agent.labels import apply_status_label, ensure_agent_labels
from agent.notify import EscalationNotice, mention_from_config
from agent.policy import classify_control_plane_error
from agent.pr import build_pr_body, build_pr_title
from agent.reconcile import reconcile_open_pull
from agent.scope import check_scope, validate_spec_scope_policy
from agent.spec import (
    TaskSpec,
    bind_spec_identity,
    is_canonical_spec_path,
    parse_spec,
)
from agent.summary import render_summary, write_github_summary
from agent.workunit import WorkUnitOutcome, WorkUnitReport, file_sha256, load_work_unit_report


class DeliveryOutcome(StrEnum):
    PR_CREATED = "PR_CREATED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


def _as_delivery_outcome(value: object) -> DeliveryOutcome:
    if isinstance(value, DeliveryOutcome):
        return value
    if isinstance(value, StrEnum):
        raise AgentError.invalid_input(
            f"invalid delivery outcome: {value!r}",
            code="INVALID_DELIVERY_RESULT",
        )
    if isinstance(value, str):
        try:
            return DeliveryOutcome(value)
        except ValueError:
            pass
    raise AgentError.invalid_input(
        f"invalid delivery outcome: {value!r}",
        code="INVALID_DELIVERY_RESULT",
    )


def _as_failure_class(value: object) -> FailureClass | None:
    if value is None:
        return None
    if isinstance(value, FailureClass):
        return value
    if isinstance(value, str):
        try:
            return FailureClass(value)
        except ValueError:
            pass
    raise AgentError.invalid_input(
        f"invalid delivery failure class: {value!r}",
        code="INVALID_DELIVERY_RESULT",
    )


def validate_delivery_result(result: DeliveryResult) -> None:
    if not isinstance(result.outcome, DeliveryOutcome):
        raise AgentError.invalid_input(
            f"delivery outcome is not DeliveryOutcome: {result.outcome!r}",
            code="INVALID_DELIVERY_RESULT",
        )
    if result.code is not None and (not isinstance(result.code, str) or result.code == ""):
        raise AgentError.invalid_input(
            "delivery result code must be a non-empty string when set",
            code="INVALID_DELIVERY_RESULT",
        )
    if result.outcome is DeliveryOutcome.PR_CREATED:
        if result.failure_class is not None:
            raise AgentError.invalid_input(
                "PR_CREATED must not set a failure class",
                code="INVALID_DELIVERY_RESULT",
            )
        return
    if result.outcome is DeliveryOutcome.FAILED:
        if result.failure_class is not FailureClass.ENVIRONMENT_FAILURE:
            raise AgentError.invalid_input(
                "FAILED delivery result requires ENVIRONMENT_FAILURE",
                code="INVALID_DELIVERY_RESULT",
            )
        return
    if result.outcome is DeliveryOutcome.ESCALATED:
        if result.failure_class is not FailureClass.ESCALATION_REQUIRED:
            raise AgentError.invalid_input(
                "ESCALATED delivery result requires ESCALATION_REQUIRED",
                code="INVALID_DELIVERY_RESULT",
            )
        return
    raise AgentError.invalid_input(
        f"unsupported delivery outcome: {result.outcome!r}",
        code="INVALID_DELIVERY_RESULT",
    )


@dataclass
class DeliveryResult:
    outcome: DeliveryOutcome
    pr_url: str | None
    pr_number: int | None
    commit_sha: str | None
    notice: EscalationNotice | None
    summary: str
    message: str
    code: str | None = None
    failure_class: FailureClass | None = None

    def __post_init__(self) -> None:
        self.outcome = _as_delivery_outcome(self.outcome)
        self.failure_class = _as_failure_class(self.failure_class)
        validate_delivery_result(self)

    def to_json_dict(self) -> dict[str, Any]:
        validate_delivery_result(self)
        return {
            "outcome": self.outcome.value,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "commit_sha": self.commit_sha,
            "notice": None if self.notice is None else self.notice.to_json_dict(),
            "message": self.message,
            "code": self.code,
        }


def assert_commit_allowed(report: WorkUnitReport) -> None:
    if report.outcome is WorkUnitOutcome.SCOPE_VIOLATION:
        raise AgentError.policy_violation(
            "no commit on scope violation",
            code="COMMIT_SCOPE_VIOLATION",
        )
    if report.outcome is not WorkUnitOutcome.FINAL_VERIFICATION_PASSED:
        raise AgentError.policy_violation(
            "commit only after validation",
            code="COMMIT_BEFORE_VALIDATION",
        )


def assert_pr_allowed(report: WorkUnitReport) -> None:
    assert_commit_allowed(report)


def assert_report_matches_spec(
    spec: TaskSpec, report: WorkUnitReport, *, spec_directory: str
) -> None:
    if not report.spec_sha256:
        raise AgentError.escalation_required(
            "report spec_sha256 is missing",
            code="SPEC_IDENTITY_MISMATCH",
        )
    if not is_canonical_spec_path(report.spec_path, spec_directory=spec_directory):
        raise AgentError.escalation_required(
            f"report spec_path is not canonical: {report.spec_path!r}",
            code="SPEC_IDENTITY_MISMATCH",
        )
    if (
        report.spec_id != spec.id
        or report.spec_path != (spec.source_path or "")
        or report.spec_sha256 != spec.spec_sha256
    ):
        raise AgentError.escalation_required(
            "WorkUnitReport Task Spec identity does not match the current spec",
            code="SPEC_IDENTITY_MISMATCH",
        )
    if report.branch != spec.target_branch:
        raise AgentError.escalation_required(
            f"report branch {report.branch!r} does not match target_branch {spec.target_branch!r}",
            code="REPORT_BRANCH_MISMATCH",
        )
    if not report.base_sha.strip():
        raise AgentError.escalation_required(
            "report base_sha is missing",
            code="BASE_SHA_MISSING",
        )


def assert_patch_digest(report_dir: Path | str, report: WorkUnitReport) -> None:
    patch_path = Path(report_dir) / report.patch_file
    if not patch_path.is_file():
        raise AgentError.escalation_required(
            f"patch file not found: {patch_path}",
            code="PATCH_DIGEST_MISMATCH",
        )
    digest = file_sha256(patch_path)
    if not report.patch_sha256:
        raise AgentError.escalation_required(
            "report patch_sha256 is missing",
            code="PATCH_DIGEST_MISMATCH",
        )
    if digest != report.patch_sha256:
        raise AgentError.escalation_required(
            "patch digest does not match report.patch_sha256",
            code="PATCH_DIGEST_MISMATCH",
        )


def run_delivery(
    spec: TaskSpec | Path | str,
    *,
    repo_root: Path | str,
    report_dir: Path | str,
    config: AgentConfig | None = None,
    github: GitHubClient | None = None,
    summary_path: Path | str | None = None,
) -> DeliveryResult:
    cfg = config or load_config()
    root = Path(repo_root)
    parsed = spec if isinstance(spec, TaskSpec) else parse_spec(spec)
    validate_spec_scope_policy(parsed, cfg.runtime_edit_policy)
    parsed = bind_spec_identity(
        parsed,
        repo_root=root,
        spec_directory=cfg.task_spec.directory,
    )
    report = load_work_unit_report(report_dir, spec=parsed)
    client = github
    try:
        if client is None:
            client = github_client_from_env()
        result = _deliver(parsed, root, Path(report_dir), report, cfg, client)
    except Exception as exc:
        classification = classify_control_plane_error(exc)
        result = _failure_result(parsed, report, exc, classification, cfg)
    markdown = _summary_markdown(parsed, report, result)
    target = summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        write_github_summary(target, markdown)
    result.summary = markdown
    _emit_delivery_terminal(result, parsed, report)
    emit(
        WORKFLOW_COMPLETED,
        result.message,
        task_id=parsed.id,
        state=report.state.state.value,
        extra={"outcome": result.outcome.value},
    )
    if result.outcome in {DeliveryOutcome.FAILED, DeliveryOutcome.ESCALATED}:
        _notify_best_effort(client, parsed, result, cfg)
    return result


def _deliver(
    spec: TaskSpec,
    root: Path,
    report_dir: Path,
    report: WorkUnitReport,
    cfg: AgentConfig,
    github: GitHubClient,
) -> DeliveryResult:
    assert_report_matches_spec(spec, report, spec_directory=cfg.task_spec.directory)
    assert_patch_digest(report_dir, report)
    ensure_agent_labels(github)
    existing = reconcile_open_pull(spec, github)
    if existing.action == "reuse":
        pull = existing.pull
        assert pull is not None
        number = int(pull["number"])
        url = str(pull.get("html_url") or "")
        apply_status_label(github, number, "agent:review")
        return DeliveryResult(
            outcome=DeliveryOutcome.PR_CREATED,
            pr_url=url or None,
            pr_number=number,
            commit_sha=None,
            notice=None,
            summary="",
            message="reused existing pull request",
        )

    if report.outcome is not WorkUnitOutcome.FINAL_VERIFICATION_PASSED:
        return _report_failure(spec, report, cfg)

    assert_pr_allowed(report)
    checkout_delivery_parent(root, spec.target_branch, report.base_sha)
    if head_sha(root) != report.base_sha:
        raise AgentError.escalation_required(
            f"HEAD {head_sha(root)} does not match report base_sha {report.base_sha}",
            code="BASE_SHA_MISMATCH",
        )
    assert_clean_for_delivery(root)
    apply_patch(root, report_dir / report.patch_file)
    emit(
        DELIVERY_VALIDATION_STARTED,
        "delivery verification started",
        task_id=spec.id,
        state=report.state.state.value,
    )
    actual_changes = collect_changes(root, report.base_sha)
    actual_paths = change_path_list(actual_changes)
    scope = check_scope(spec, actual_changes, cfg.runtime_edit_policy)
    if not scope.allowed:
        raise AgentError.policy_violation(
            "scope violation after patch apply: " + ", ".join(scope.violation_paths),
            code="COMMIT_SCOPE_VIOLATION",
        )
    expected_paths = tuple(report.changed_files)
    if set(actual_paths) != set(expected_paths):
        raise AgentError.escalation_required(
            "applied patch paths do not match report.changed_files",
            code="PATCH_MANIFEST_MISMATCH",
        )
    records = run_final_verification(spec, repo_root=root, config=cfg)
    failed = next((record for record in records if not record.passed), None)
    if failed is not None:
        classification = classify_validation(failed) or FailureClass.ESCALATION_REQUIRED
        if classification is FailureClass.ENVIRONMENT_FAILURE:
            raise AgentError.environment_failure(
                "final verification failed after patch apply",
                code="DELIVER_FINAL_VERIFICATION_FAILED",
            )
        raise AgentError.escalation_required(
            "final verification failed after patch apply",
            code="DELIVER_FINAL_VERIFICATION_FAILED",
        )
    emit(
        DELIVERY_VALIDATION_PASSED,
        "delivery verification passed",
        task_id=spec.id,
        state=report.state.state.value,
    )
    commit_sha = commit_paths(
        root,
        list(actual_paths),
        _commit_message(spec, report),
    )
    push_branch(root, spec.target_branch)
    body = build_pr_body(
        spec,
        completed_tasks=report.completed_tasks,
        changed_files=actual_paths,
        validation_results=report.validation_results,
        final_verification="PASSED",
        repair_attempts=report.repair_attempts,
    )
    try:
        created = github.create_pull(
            title=build_pr_title(spec),
            head=spec.target_branch,
            base=spec.base_branch,
            body=body,
        )
    except AgentError as exc:
        if exc.code != "GITHUB_API_VALIDATION":
            raise
        raced = reconcile_open_pull(spec, github)
        if raced.action != "reuse" or raced.pull is None:
            raise
        created = raced.pull
    number = int(created["number"])
    url = str(created.get("html_url") or "")
    apply_status_label(github, number, "agent:review")
    emit(PR_CREATED, url or f"pull request #{number}", task_id=spec.id, state="PR_CREATED")
    return DeliveryResult(
        outcome=DeliveryOutcome.PR_CREATED,
        pr_url=url or None,
        pr_number=number,
        commit_sha=commit_sha,
        notice=None,
        summary="",
        message="created pull request",
        code=None,
    )


def _commit_message(spec: TaskSpec, report: WorkUnitReport) -> str:
    tasks = ", ".join(report.completed_tasks) or "tasks"
    return f"feat({spec.id}): complete {tasks}"


def _report_failure(
    spec: TaskSpec,
    report: WorkUnitReport,
    cfg: AgentConfig,
) -> DeliveryResult:
    classification = FailureClass.ESCALATION_REQUIRED
    if report.outcome is WorkUnitOutcome.FAILED:
        classification = FailureClass.ENVIRONMENT_FAILURE
    if report.failure_class is FailureClass.ENVIRONMENT_FAILURE:
        classification = FailureClass.ENVIRONMENT_FAILURE
    notice = _notice_from_report(spec, report, report.message, classification, cfg)
    outcome = (
        DeliveryOutcome.FAILED
        if classification is FailureClass.ENVIRONMENT_FAILURE
        else DeliveryOutcome.ESCALATED
    )
    return DeliveryResult(
        outcome=outcome,
        pr_url=None,
        pr_number=None,
        commit_sha=None,
        notice=notice,
        summary="",
        message=report.message,
        code=report.code,
        failure_class=classification,
    )


def _failure_result(
    spec: TaskSpec,
    report: WorkUnitReport,
    error: BaseException,
    classification: FailureClass,
    cfg: AgentConfig,
) -> DeliveryResult:
    outcome = (
        DeliveryOutcome.FAILED
        if classification is FailureClass.ENVIRONMENT_FAILURE
        else DeliveryOutcome.ESCALATED
    )
    code = error.code if isinstance(error, AgentError) else None
    return DeliveryResult(
        outcome=outcome,
        pr_url=None,
        pr_number=None,
        commit_sha=None,
        notice=_notice_from_report(spec, report, str(error), classification, cfg),
        summary="",
        message=str(error),
        code=code,
        failure_class=classification,
    )


def _notice_from_report(
    spec: TaskSpec,
    report: WorkUnitReport,
    reason: str,
    classification: FailureClass,
    cfg: AgentConfig,
) -> EscalationNotice:
    action = (
        "Re-run the workflow after the environment recovers."
        if classification is FailureClass.ENVIRONMENT_FAILURE
        else "Inspect the Task Spec, Git branch, and Execution State before continuing."
    )
    return EscalationNotice(
        task_id=spec.id,
        current_task=report.current_task,
        reason=reason,
        last_validation=report.state.last_validation,
        repair_attempts=report.repair_attempts,
        required_human_action=action,
        mention=mention_from_config(cfg),
    )


def _emit_delivery_terminal(result: DeliveryResult, spec: TaskSpec, report: WorkUnitReport) -> None:
    if result.outcome is DeliveryOutcome.FAILED:
        event = FAILED
    elif result.outcome is DeliveryOutcome.ESCALATED:
        event = ESCALATED
    else:
        return
    extra = None if result.code is None else {"code": result.code}
    emit(
        event,
        result.message,
        task_id=spec.id,
        state=report.state.state.value,
        extra=extra,
    )


def _notify_best_effort(
    github: GitHubClient | None,
    spec: TaskSpec,
    result: DeliveryResult,
    cfg: AgentConfig,
) -> None:
    if result.notice is None or github is None:
        return
    try:
        body = result.notice.to_markdown()
        label = "agent:failed" if result.outcome is DeliveryOutcome.FAILED else "agent:escalated"
        if result.pr_number is not None:
            _notify_pull_best_effort(github, result.pr_number, body, label, spec, result)
            return
        existing = _list_open_pulls_best_effort(github, spec, result)
        if existing is None:
            return
        if existing:
            number = int(existing[0]["number"])
            _notify_pull_best_effort(github, number, body, label, spec, result)
            return
        _create_issue_best_effort(github, spec, result, body, label)
    except Exception as exc:
        _emit_delivery_notification_failed(
            "notify", spec.id, result.outcome.value, result.code, exc
        )


def _notify_pull_best_effort(
    github: GitHubClient,
    number: int,
    body: str,
    label: str,
    spec: TaskSpec,
    result: DeliveryResult,
) -> None:
    try:
        github.create_issue_comment(number, body)
    except Exception as exc:
        _emit_delivery_notification_failed(
            "create_issue_comment", spec.id, result.outcome.value, result.code, exc
        )
    try:
        apply_status_label(github, number, label)
    except Exception as exc:
        _emit_delivery_notification_failed(
            "apply_status_label", spec.id, result.outcome.value, result.code, exc
        )


def _list_open_pulls_best_effort(
    github: GitHubClient,
    spec: TaskSpec,
    result: DeliveryResult,
) -> list[dict[str, Any]] | None:
    try:
        return github.list_open_pulls(head_branch=spec.target_branch)
    except Exception as exc:
        _emit_delivery_notification_failed(
            "list_open_pulls", spec.id, result.outcome.value, result.code, exc
        )
        return None


def _create_issue_best_effort(
    github: GitHubClient,
    spec: TaskSpec,
    result: DeliveryResult,
    body: str,
    label: str,
) -> None:
    try:
        github.create_issue(
            title=f"{spec.id}: agent {result.outcome.value.lower()}",
            body=body,
            labels=[label],
        )
    except Exception as exc:
        _emit_delivery_notification_failed(
            "create_issue", spec.id, result.outcome.value, result.code, exc
        )


def _emit_delivery_notification_failed(
    operation: str,
    task_id: str,
    primary_outcome: str,
    primary_code: str | None,
    error: BaseException,
) -> None:
    emit_notification_failed_best_effort(
        phase="delivery",
        task_id=task_id,
        primary_outcome=primary_outcome,
        primary_code=primary_code,
        operation=operation,
        error=error,
    )


def _summary_markdown(spec: TaskSpec, report: WorkUnitReport, result: DeliveryResult) -> str:
    detail = result.message
    if result.code:
        detail = f"{result.code}: {result.message}"
    state = (
        report.state.state.value
        if result.outcome is not DeliveryOutcome.PR_CREATED
        else "PR_CREATED"
    )
    return render_summary(
        spec_path=report.spec_path or spec.id,
        task_id=spec.id,
        state=state,
        current_task=report.current_task,
        completed_tasks=report.completed_tasks,
        changed_files=report.changed_files,
        validation_results=report.validation_results,
        repair_attempts=report.repair_attempts,
        pr_url=result.pr_url,
        failure_reason=detail if result.outcome is DeliveryOutcome.FAILED else None,
        escalation_reason=detail if result.outcome is DeliveryOutcome.ESCALATED else None,
    )
