"""CodeRabbit terminal evidence from GitHub Checks and commit statuses.

Wake-up event payloads are not the source of truth. This module re-fetches
check runs and commit statuses for an exact HEAD SHA, then keeps only
entries whose app slug, status context, or creator login matches config.

Live COMPLETED/SKIPPED payloads could not be captured here (GitHub auth 401).
Both GitHub transports are therefore collected: Checks (`review_progress`)
and legacy commit statuses (`reviews.commit_status`). The latest matching
completed item on the requested SHA wins. An in-progress item on that SHA
blocks READY.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from agent.config import CodeRabbitConfig
from agent.github_api import GitHubClient
from agent.review_filter import is_configured_actor

KIND_COMPLETED = "CODERABBIT_COMPLETED"
KIND_SKIPPED = "CODERABBIT_SKIPPED"
KIND_FAILED = "CODERABBIT_FAILED"
KIND_IN_PROGRESS = "IN_PROGRESS"
KIND_NONE = "NONE"

CHECK_SUCCESS = "success"
CHECK_SKIPPED_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
CHECK_FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "stale", "error"}
)
CHECK_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
STATUS_SUCCESS = "success"
STATUS_FAILED_STATES = frozenset({"failure", "error"})
STATUS_PENDING = "pending"
TERMINAL_STATUS_STATES = frozenset({"success", "failure", "error"})


class CodeRabbitTerminalKind(StrEnum):
    NONE = KIND_NONE
    IN_PROGRESS = KIND_IN_PROGRESS
    COMPLETED = KIND_COMPLETED
    SKIPPED = KIND_SKIPPED
    FAILED = KIND_FAILED


@dataclass(frozen=True)
class CodeRabbitTerminal:
    kind: CodeRabbitTerminalKind
    source: str
    head_sha: str
    conclusion: str
    observed_at: str

    def is_completed(self) -> bool:
        return self.kind is CodeRabbitTerminalKind.COMPLETED

    def is_escalating(self) -> bool:
        return self.kind in {CodeRabbitTerminalKind.SKIPPED, CodeRabbitTerminalKind.FAILED}

    def escalation_code(self) -> str:
        if self.kind is CodeRabbitTerminalKind.SKIPPED:
            return "CODERABBIT_SKIPPED"
        return "CODERABBIT_REVIEW_FAILED"

    def to_json_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "head_sha": self.head_sha,
            "conclusion": self.conclusion,
            "observed_at": self.observed_at,
        }


def none_terminal(head_sha: str) -> CodeRabbitTerminal:
    return CodeRabbitTerminal(
        kind=CodeRabbitTerminalKind.NONE,
        source="",
        head_sha=head_sha,
        conclusion="",
        observed_at="",
    )


def status_context_matches(context: str, configured: str) -> bool:
    value = context.strip()
    expected = configured.strip()
    if not value or not expected:
        return False
    return value == expected or value.startswith(f"{expected}/") or value.startswith(f"{expected} ")


def check_app_matches(payload: dict[str, Any], configured_slug: str) -> bool:
    slug = configured_slug.strip()
    if not slug:
        return False
    app = payload.get("app")
    if not isinstance(app, dict):
        return False
    return str(app.get("slug") or "").strip() == slug


def has_coderabbit_event_identity(payload: dict[str, Any], cfg: CodeRabbitConfig) -> bool:
    sender = payload.get("sender")
    login = sender.get("login") if isinstance(sender, dict) else None
    if is_configured_actor(login if isinstance(login, str) else None, cfg.actor):
        return True
    check_run = payload.get("check_run")
    if isinstance(check_run, dict) and check_app_matches(check_run, cfg.check_app_slug):
        return True
    context = payload.get("context")
    if isinstance(context, str) and status_context_matches(context, cfg.status_context):
        return True
    return False


def is_terminal_wakeup_event(payload: dict[str, Any]) -> bool:
    """True for completed Checks or non-pending commit statuses. Ignores comment bodies."""
    check_run = payload.get("check_run")
    if isinstance(check_run, dict):
        status = str(check_run.get("status") or "").strip().lower()
        return status == "completed"
    state = payload.get("state")
    if isinstance(state, str):
        return state.strip().lower() in TERMINAL_STATUS_STATES
    return False


def event_commit_sha(payload: dict[str, Any]) -> str:
    check_run = payload.get("check_run")
    if isinstance(check_run, dict):
        sha = check_run.get("head_sha")
        if isinstance(sha, str) and sha.strip():
            return sha.strip()
    sha = payload.get("sha")
    if isinstance(sha, str) and sha.strip():
        return sha.strip()
    pull = payload.get("pull_request")
    if isinstance(pull, dict):
        head = pull.get("head")
        if isinstance(head, dict):
            value = head.get("sha")
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def collect_coderabbit_terminal(
    client: GitHubClient,
    head_sha: str,
    cfg: CodeRabbitConfig,
) -> CodeRabbitTerminal:
    checks = client.list_check_runs_for_ref(head_sha)
    statuses = client.list_commit_statuses_for_ref(head_sha)
    return resolve_coderabbit_terminal(checks, statuses, head_sha=head_sha, cfg=cfg)


def resolve_coderabbit_terminal(
    check_runs: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    *,
    head_sha: str,
    cfg: CodeRabbitConfig,
) -> CodeRabbitTerminal:
    expected = head_sha.strip()
    if not expected:
        return none_terminal("")
    matching: list[tuple[str, str, str, str]] = []
    active = False
    for check in check_runs:
        if not _check_belongs(check, expected, cfg):
            continue
        status = str(check.get("status") or "").strip().lower()
        if status in CHECK_ACTIVE_STATUSES:
            active = True
            continue
        if status and status != "completed":
            continue
        conclusion = str(check.get("conclusion") or "").strip().lower()
        matching.append(
            (
                str(check.get("completed_at") or check.get("started_at") or ""),
                "check_run",
                conclusion,
                _map_check_conclusion(conclusion).value,
            )
        )
    for status in statuses:
        if not _status_belongs(status, cfg):
            continue
        state = str(status.get("state") or "").strip().lower()
        if state == STATUS_PENDING:
            active = True
            continue
        matching.append(
            (
                str(status.get("updated_at") or status.get("created_at") or ""),
                "commit_status",
                state,
                _map_status_state(state).value,
            )
        )
    if active:
        return CodeRabbitTerminal(
            kind=CodeRabbitTerminalKind.IN_PROGRESS,
            source="",
            head_sha=expected,
            conclusion="in_progress",
            observed_at="",
        )
    if not matching:
        return none_terminal(expected)
    matching.sort(key=lambda item: item[0])
    observed_at, source, conclusion, kind_value = matching[-1]
    return CodeRabbitTerminal(
        kind=CodeRabbitTerminalKind(kind_value),
        source=source,
        head_sha=expected,
        conclusion=conclusion,
        observed_at=observed_at,
    )


def _check_belongs(check: dict[str, Any], head_sha: str, cfg: CodeRabbitConfig) -> bool:
    sha = str(check.get("head_sha") or "").strip()
    if sha and sha != head_sha:
        return False
    return check_app_matches(check, cfg.check_app_slug)


def _status_belongs(status: dict[str, Any], cfg: CodeRabbitConfig) -> bool:
    context = status.get("context")
    if isinstance(context, str) and status_context_matches(context, cfg.status_context):
        return True
    creator = status.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    return is_configured_actor(login if isinstance(login, str) else None, cfg.actor)


def _map_check_conclusion(conclusion: str) -> CodeRabbitTerminalKind:
    if conclusion == CHECK_SUCCESS:
        return CodeRabbitTerminalKind.COMPLETED
    if conclusion in CHECK_SKIPPED_CONCLUSIONS:
        return CodeRabbitTerminalKind.SKIPPED
    if conclusion in CHECK_FAILED_CONCLUSIONS:
        return CodeRabbitTerminalKind.FAILED
    return CodeRabbitTerminalKind.FAILED


def _map_status_state(state: str) -> CodeRabbitTerminalKind:
    if state == STATUS_SUCCESS:
        return CodeRabbitTerminalKind.COMPLETED
    if state in STATUS_FAILED_STATES:
        return CodeRabbitTerminalKind.FAILED
    return CodeRabbitTerminalKind.FAILED
