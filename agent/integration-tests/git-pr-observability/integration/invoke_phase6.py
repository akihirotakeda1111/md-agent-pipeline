"""Thin Production binding for the Phase 6 integration suite.

Translates test DTOs and injects Fake Codex / Fake GitHub. Does not implement
Scope, Validation, Repair, Delivery, Reconciliation, or failure classification.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent.codex_runner import ProcessResult
from agent.config import load_config
from agent.delivery import run_delivery
from agent.labels import apply_status_label
from agent.notify import EscalationNotice, mention_from_config
from agent.workunit import run_work_unit

from .harness.adapters import (
    ArtifactBundle,
    DeliveryRequest,
    DeliveryResult,
    NotificationRequest,
    Phase6FlowRequest,
    Phase6FlowResult,
    ServiceBundle,
    WorkUnitRequest,
    WorkUnitResult,
)
from .harness.fake_github import FakeGitHub
from .harness.observations import ObservationLog

_JOB_CREDENTIALS = (
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
)


class _FakeGitHubClient:
    """Maps Production GitHubClient methods onto the recording FakeGitHub."""

    def __init__(self, fake: FakeGitHub) -> None:
        self._fake = fake

    def list_open_pulls(self, *, head_branch: str) -> list[dict[str, Any]]:
        payload = self._fake.list_pull_requests(head_branch=head_branch)
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise TypeError("FakeGitHub list_pull_requests must return a list")
        return payload

    def create_pull(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        payload = self._fake.create_pull_request(title=title, head=head, base=base, body=body) or {}
        if not isinstance(payload, dict):
            payload = {}
        result = dict(payload)
        if "html_url" not in result and "url" in result:
            result["html_url"] = result["url"]
        result.setdefault("number", 1)
        result.setdefault("html_url", "")
        return result

    def get_label(self, name: str) -> dict[str, Any] | None:
        payload = self._fake.request("get_label", name=name)
        return payload if isinstance(payload, dict) else None

    def create_label(self, *, name: str, color: str, description: str) -> dict[str, Any]:
        payload = self._fake.ensure_label(name=name, color=color, description=description)
        if isinstance(payload, dict):
            return payload
        return {"name": name, "color": color, "description": description}

    def add_issue_labels(self, issue_number: int, labels: list[str]) -> None:
        self._fake.set_labels(issue_number=issue_number, labels=labels)

    def create_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        payload = self._fake.create_issue(title=title, body=body, labels=labels)
        return payload if isinstance(payload, dict) else {"title": title, "body": body}

    def create_issue_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        payload = self._fake.add_pr_comment(issue_number=issue_number, body=body)
        return payload if isinstance(payload, dict) else {"body": body}


def _ingest_stdout_events(text: str, observations: ObservationLog) -> None:
    for line in text.splitlines():
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and ("event" in record or "type" in record):
            observations.emit(record)


@contextmanager
def _capture_events(observations: ObservationLog) -> Iterator[None]:
    buffer = io.StringIO()
    original = sys.stdout

    class _Tee:
        def write(self, data: str) -> int:
            original.write(data)
            return buffer.write(data)

        def flush(self) -> None:
            original.flush()
            buffer.flush()

        def __getattr__(self, name: str) -> Any:
            return getattr(original, name)

    sys.stdout = _Tee()  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout = original
        _ingest_stdout_events(buffer.getvalue(), observations)


@contextmanager
def _isolated_job_environ(updates: dict[str, str]) -> Iterator[dict[str, str]]:
    """Apply one GHA job's secrets, then restore the process environment completely."""
    snapshot = dict(os.environ)
    try:
        for key in _JOB_CREDENTIALS:
            os.environ.pop(key, None)
        os.environ.update(updates)
        yield dict(os.environ)
    finally:
        added = set(os.environ) - set(snapshot)
        for key in added:
            os.environ.pop(key, None)
        os.environ.update(snapshot)


def _execute_job_env(environment: dict[str, str]) -> dict[str, str]:
    env = dict(environment)
    env["GITHUB_TOKEN"] = ""
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_PAT", None)
    return env


def _deliver_job_env(environment: dict[str, str]) -> dict[str, str]:
    env = dict(environment)
    env.pop("CODEX_API_KEY", None)
    env.pop("OPENAI_API_KEY", None)
    return env


def _reset_worktree_like_fresh_checkout(repo_root: Path) -> None:
    """GHA deliver starts from a clean checkout, not the execute job's dirty tree."""
    env = dict(os.environ)
    for argv in (["git", "reset", "--hard", "HEAD"], ["git", "clean", "-fd"]):
        completed = subprocess.run(
            argv, cwd=repo_root, env=env, capture_output=True, text=True, check=False
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(f"{' '.join(argv)} failed: {detail}")


def _codex_executor(services: ServiceBundle):
    def executor(
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
        stdin: str,
    ) -> ProcessResult:
        result = services.codex.invoke(workspace=Path(cwd), env=dict(env), argv=list(command))
        message = str(result.get("final_message") or "")
        if "--output-last-message" in command:
            index = command.index("--output-last-message")
            Path(command[index + 1]).write_text(message, encoding="utf-8")
        return ProcessResult(returncode=int(result["exit_code"]), stdout=message, stderr="")

    return executor


def _config_with_mention(mention: str | None):
    cfg = load_config()
    if mention is None:
        return cfg
    return replace(cfg, notification=replace(cfg.notification, mention=mention))


def _work_unit_status(outcome: str) -> str:
    if outcome == "FINAL_VERIFICATION_PASSED":
        return "PASSED"
    return outcome


def _delivery_status(outcome: str) -> str:
    if outcome == "PR_CREATED":
        return "READY"
    return outcome


def _delivery_reason(result: Any) -> str | None:
    code = getattr(result, "code", None)
    if code:
        return str(code)
    message = getattr(result, "message", None)
    return str(message) if message else None


def _report_dir(request: WorkUnitRequest | DeliveryRequest | Phase6FlowRequest) -> Path:
    if isinstance(request, DeliveryRequest):
        return request.artifacts.report_path.parent
    return Path(request.repo_root).parent / "agent-report"


class ProductionPhase6Driver:
    def run_work_unit(self, request: WorkUnitRequest, services: ServiceBundle) -> WorkUnitResult:
        report_dir = _report_dir(request)
        report_dir.mkdir(parents=True, exist_ok=True)
        with (
            _isolated_job_environ(_execute_job_env(request.environment)) as env,
            _capture_events(services.observations),
        ):
            report = run_work_unit(
                request.spec_path,
                repo_root=request.repo_root,
                report_dir=report_dir,
                config=_config_with_mention(None),
                env=env,
                executor=_codex_executor(services),
                persist_state=request.persist_state,
            )
        artifacts = ArtifactBundle(
            report_path=report_dir / "report.json",
            patch_path=report_dir / report.patch_file,
        )
        return WorkUnitResult(
            status=_work_unit_status(report.outcome),
            reason=report.message or report.classification,
            artifacts=artifacts,
            current_task=report.current_task,
            completed_tasks=list(report.completed_tasks),
            repair_attempts=report.repair_attempts,
            events=list(services.observations.events),
            summary=report.message,
        )

    def deliver(self, request: DeliveryRequest, services: ServiceBundle) -> DeliveryResult:
        cfg = _config_with_mention(request.notification_mention)
        github = _FakeGitHubClient(services.github)
        with (
            _isolated_job_environ(_deliver_job_env(request.environment)),
            _capture_events(services.observations),
        ):
            result = run_delivery(
                request.spec_path,
                repo_root=request.repo_root,
                report_dir=request.artifacts.report_path.parent,
                config=cfg,
                github=github,  # type: ignore[arg-type]
            )
        return DeliveryResult(
            status=_delivery_status(result.outcome),
            reason=_delivery_reason(result),
            pr_url=result.pr_url,
            reused=result.message == "reused existing pull request",
            events=list(services.observations.events),
            summary=result.summary,
        )

    def publish_outcome(self, request: NotificationRequest, services: ServiceBundle) -> None:
        cfg = _config_with_mention(request.mention)
        github = _FakeGitHubClient(services.github)
        notice = EscalationNotice(
            task_id=request.task_id,
            current_task=request.current_task,
            reason=request.reason,
            last_validation=request.last_validation,
            repair_attempts=request.repair_attempts,
            required_human_action=request.required_human_action,
            mention=mention_from_config(cfg),
        )
        body = notice.to_markdown()
        label = "agent:failed" if request.status.upper() == "FAILED" else "agent:escalated"
        if request.pr_number is not None:
            github.create_issue_comment(request.pr_number, body)
            apply_status_label(github, request.pr_number, label)  # type: ignore[arg-type]
            return
        github.create_issue(
            title=f"{request.task_id}: agent {request.status.lower()}",
            body=body,
            labels=[label],
        )

    def run_phase6_flow(
        self, request: Phase6FlowRequest, services: ServiceBundle
    ) -> Phase6FlowResult:
        work = self.run_work_unit(
            WorkUnitRequest(
                spec_path=request.spec_path,
                repo_root=request.repo_root,
                persist_state=False,
                environment=request.execute_environment,
            ),
            services,
        )
        _reset_worktree_like_fresh_checkout(Path(request.repo_root))
        artifacts = work.artifacts
        if artifacts is None:
            report_dir = _report_dir(request)
            artifacts = ArtifactBundle(
                report_path=report_dir / "report.json",
                patch_path=report_dir / "changes.patch",
            )
        delivery = self.deliver(
            DeliveryRequest(
                spec_path=request.spec_path,
                repo_root=request.repo_root,
                artifacts=artifacts,
                environment=request.deliver_environment,
                notification_mention=request.notification_mention,
            ),
            services,
        )
        status = delivery.status
        if work.status.upper() not in {"PASSED"} and delivery.status.upper() == "READY":
            status = work.status
        return Phase6FlowResult(
            status=status,
            reason=delivery.reason or work.reason,
            pr_url=delivery.pr_url,
            current_task=work.current_task,
            completed_tasks=work.completed_tasks,
            repair_attempts=work.repair_attempts,
            events=list(services.observations.events),
            summary=delivery.summary,
        )


def create_driver() -> ProductionPhase6Driver:
    return ProductionPhase6Driver()
