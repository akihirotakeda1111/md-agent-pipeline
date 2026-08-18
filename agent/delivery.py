"""Commit, push, and open a Pull Request after Final Verification.

Codex credentials must never be present in this process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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
from agent.scope import check_scope
from agent.spec import TaskSpec, parse_spec
from agent.summary import render_summary, write_github_summary
from agent.workunit import WorkUnitReport, file_sha256, load_work_unit_report


@dataclass
class DeliveryResult:
    outcome: str
    pr_url: str | None
    pr_number: int | None
    commit_sha: str | None
    notice: EscalationNotice | None
    summary: str
    message: str
    code: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "commit_sha": self.commit_sha,
            "notice": None if self.notice is None else self.notice.to_json_dict(),
            "message": self.message,
            "code": self.code,
        }


def assert_commit_allowed(report: WorkUnitReport) -> None:
    if not report.scope_allowed or report.outcome == "SCOPE_VIOLATION":
        raise AgentError.policy_violation(
            "no commit on scope violation",
            code="COMMIT_SCOPE_VIOLATION",
        )
    if not report.validation_passed:
        raise AgentError.policy_violation(
            "commit only after validation",
            code="COMMIT_BEFORE_VALIDATION",
        )


def assert_pr_allowed(report: WorkUnitReport) -> None:
    assert_commit_allowed(report)
    if not report.final_verification_passed:
        raise AgentError.policy_violation(
            "no PR before final verification",
            code="PR_BEFORE_FINAL_VERIFICATION",
        )


def assert_report_matches_spec(
    spec: TaskSpec, report: WorkUnitReport, repo_root: Path | str
) -> None:
    if report.spec_id != spec.id:
        raise AgentError.escalation_required(
            f"report spec_id {report.spec_id!r} does not match spec {spec.id!r}",
            code="REPORT_SPEC_MISMATCH",
        )
    if report.branch != spec.target_branch:
        raise AgentError.escalation_required(
            f"report branch {report.branch!r} does not match target_branch {spec.target_branch!r}",
            code="REPORT_BRANCH_MISMATCH",
        )
    expected = _normalize_spec_path(spec.source_path or spec.id, repo_root)
    actual = _normalize_spec_path(report.spec_path or spec.id, repo_root)
    if expected != actual:
        raise AgentError.escalation_required(
            f"report spec_path {report.spec_path!r} does not match {spec.source_path!r}",
            code="REPORT_SPEC_MISMATCH",
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


def _normalize_spec_path(path: str, repo_root: Path | str) -> str:
    raw = Path(path)
    root = Path(repo_root).resolve()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        return candidate.resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return Path(path).as_posix().replace("\\", "/")


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
    report = load_work_unit_report(report_dir)
    client = github
    try:
        if client is None:
            client = github_client_from_env()
        result = _deliver(parsed, root, Path(report_dir), report, cfg, client)
    except Exception as exc:
        classification = classify_control_plane_error(exc)
        result = _failure_result(parsed, report, exc, classification, cfg)
        _notify(client, parsed, result, cfg)
    markdown = _summary_markdown(parsed, report, result)
    target = summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        write_github_summary(target, markdown)
    result.summary = markdown
    emit(
        WORKFLOW_COMPLETED,
        result.message,
        task_id=parsed.id,
        state=report.state.state.value,
        extra={"outcome": result.outcome},
    )
    return result


def _deliver(
    spec: TaskSpec,
    root: Path,
    report_dir: Path,
    report: WorkUnitReport,
    cfg: AgentConfig,
    github: GitHubClient,
) -> DeliveryResult:
    assert_report_matches_spec(spec, report, root)
    assert_patch_digest(report_dir, report)
    ensure_agent_labels(github)
    existing = reconcile_open_pull(spec, github)
    if existing.action == "reuse":
        pull = existing.pull
        assert pull is not None
        number = int(pull["number"])
        url = str(pull.get("html_url") or "")
        apply_status_label(github, number, "agent:ready")
        return DeliveryResult(
            outcome="PR_CREATED",
            pr_url=url or None,
            pr_number=number,
            commit_sha=None,
            notice=None,
            summary="",
            message="reused existing pull request",
        )

    if (
        report.outcome in {"FAILED", "ESCALATED", "SCOPE_VIOLATION"}
        or not report.final_verification_passed
    ):
        return _report_failure(spec, report, cfg, github)

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
    scope = check_scope(spec, actual_changes)
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
    apply_status_label(github, number, "agent:ready")
    emit(PR_CREATED, url or f"pull request #{number}", task_id=spec.id, state="PR_CREATED")
    return DeliveryResult(
        outcome="PR_CREATED",
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
    github: GitHubClient,
) -> DeliveryResult:
    classification = FailureClass.ESCALATION_REQUIRED
    if report.outcome == "FAILED":
        classification = FailureClass.ENVIRONMENT_FAILURE
    if report.classification == FailureClass.ENVIRONMENT_FAILURE.value:
        classification = FailureClass.ENVIRONMENT_FAILURE
    notice = _notice_from_report(spec, report, report.message, classification, cfg)
    result = DeliveryResult(
        outcome="FAILED" if classification is FailureClass.ENVIRONMENT_FAILURE else "ESCALATED",
        pr_url=None,
        pr_number=None,
        commit_sha=None,
        notice=notice,
        summary="",
        message=report.message,
        code=report.classification,
    )
    _notify(github, spec, result, cfg)
    event = FAILED if result.outcome == "FAILED" else ESCALATED
    emit(event, report.message, task_id=spec.id, state=report.state.state.value)
    return result


def _failure_result(
    spec: TaskSpec,
    report: WorkUnitReport,
    error: BaseException,
    classification: FailureClass,
    cfg: AgentConfig,
) -> DeliveryResult:
    outcome = "FAILED" if classification is FailureClass.ENVIRONMENT_FAILURE else "ESCALATED"
    event = FAILED if outcome == "FAILED" else ESCALATED
    emit(event, str(error), task_id=spec.id, state=report.state.state.value)
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


def _notify(
    github: GitHubClient | None,
    spec: TaskSpec,
    result: DeliveryResult,
    cfg: AgentConfig,
) -> None:
    if result.notice is None or github is None:
        return
    body = result.notice.to_markdown()
    label = "agent:failed" if result.outcome == "FAILED" else "agent:escalated"
    if result.pr_number is not None:
        github.create_issue_comment(result.pr_number, body)
        apply_status_label(github, result.pr_number, label)
        return
    existing = github.list_open_pulls(head_branch=spec.target_branch)
    if existing:
        number = int(existing[0]["number"])
        github.create_issue_comment(number, body)
        apply_status_label(github, number, label)
        return
    github.create_issue(
        title=f"{spec.id}: agent {result.outcome.lower()}",
        body=body,
        labels=[label],
    )


def _summary_markdown(spec: TaskSpec, report: WorkUnitReport, result: DeliveryResult) -> str:
    detail = result.message
    if result.code:
        detail = f"{result.code}: {result.message}"
    return render_summary(
        spec_path=report.spec_path or spec.id,
        task_id=spec.id,
        state=report.state.state.value if result.outcome != "PR_CREATED" else "PR_CREATED",
        current_task=report.current_task,
        completed_tasks=report.completed_tasks,
        changed_files=report.changed_files,
        validation_results=report.validation_results,
        repair_attempts=report.repair_attempts,
        pr_url=result.pr_url,
        failure_reason=detail if result.outcome == "FAILED" else None,
        escalation_reason=detail if result.outcome == "ESCALATED" else None,
    )
