"""CodeRabbit terminal evidence from GitHub Checks and commit statuses.

Wake-up event payloads are not the source of truth. This module re-fetches
check runs and commit statuses for an exact HEAD SHA, then keeps only
entries whose app slug, status context, or creator login matches config.

Live COMPLETED/SKIPPED payloads could not be captured here (GitHub auth 401).
Both GitHub transports are therefore collected after wake-up: Checks
(`review_progress`) and commit statuses (`reviews.commit_status`).

On the current HEAD, every matching Check and commit status is ordered by
timestamp. The latest item wins. An older pending / skipped status does not
override a later completed one. Commit status `state=success` is COMPLETED only
when the description says Review completed. Missing or unknown descriptions are
an ambiguous terminal and fail-closed to ESCALATED.
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
KIND_AMBIGUOUS = "CODERABBIT_AMBIGUOUS"
KIND_IN_PROGRESS = "IN_PROGRESS"
KIND_NONE = "NONE"

CHECK_SUCCESS = "success"
CHECK_SKIPPED_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
CHECK_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "stale", "error"})
CHECK_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
STATUS_SUCCESS = "success"
STATUS_FAILED_STATES = frozenset({"failure", "error"})
STATUS_PENDING = "pending"
TERMINAL_STATUS_STATES = frozenset({"success", "failure", "error"})
STATUS_DESC_COMPLETED = "review completed"
STATUS_DESC_SKIPPED = "review skipped"
STATUS_DESC_IN_PROGRESS = "review in progress"


class CodeRabbitTerminalKind(StrEnum):
    NONE = KIND_NONE
    IN_PROGRESS = KIND_IN_PROGRESS
    COMPLETED = KIND_COMPLETED
    SKIPPED = KIND_SKIPPED
    FAILED = KIND_FAILED
    AMBIGUOUS = KIND_AMBIGUOUS


@dataclass(frozen=True)
class CodeRabbitTerminal:
    kind: CodeRabbitTerminalKind
    source: str
    head_sha: str
    conclusion: str
    observed_at: str
    description: str = ""

    def is_completed(self) -> bool:
        return self.kind is CodeRabbitTerminalKind.COMPLETED

    def is_escalating(self) -> bool:
        return self.kind in {
            CodeRabbitTerminalKind.SKIPPED,
            CodeRabbitTerminalKind.FAILED,
            CodeRabbitTerminalKind.AMBIGUOUS,
        }

    def escalation_code(self) -> str:
        if self.kind is CodeRabbitTerminalKind.SKIPPED:
            return "CODERABBIT_SKIPPED"
        if self.kind is CodeRabbitTerminalKind.AMBIGUOUS:
            return "CODERABBIT_AMBIGUOUS"
        return "CODERABBIT_REVIEW_FAILED"

    def to_json_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "source": self.source,
            "head_sha": self.head_sha,
            "conclusion": self.conclusion,
            "observed_at": self.observed_at,
            "description": self.description,
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
    matching: list[tuple[str, int, str, str, str, str]] = []
    for check in check_runs:
        item = _check_timeline_item(check, expected, cfg)
        if item is not None:
            matching.append(item)
    for status in statuses:
        item = _status_timeline_item(status, expected, cfg)
        if item is not None:
            matching.append(item)
    if not matching:
        return none_terminal(expected)
    matching.sort(key=lambda row: (row[0], row[1]))
    observed_at, _, source, conclusion, kind_value, description = matching[-1]
    kind = CodeRabbitTerminalKind(kind_value)
    if kind is CodeRabbitTerminalKind.NONE:
        return none_terminal(expected)
    return CodeRabbitTerminal(
        kind=kind,
        source=source,
        head_sha=expected,
        conclusion=conclusion,
        observed_at=observed_at,
        description=description,
    )


def _check_belongs(check: dict[str, Any], head_sha: str, cfg: CodeRabbitConfig) -> bool:
    sha = str(check.get("head_sha") or "").strip()
    if sha and sha != head_sha:
        return False
    return check_app_matches(check, cfg.check_app_slug)


def _status_belongs(status: dict[str, Any], head_sha: str, cfg: CodeRabbitConfig) -> bool:
    sha = str(status.get("sha") or "").strip()
    if sha and sha != head_sha:
        return False
    context = status.get("context")
    if isinstance(context, str) and status_context_matches(context, cfg.status_context):
        return True
    creator = status.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    return is_configured_actor(login if isinstance(login, str) else None, cfg.actor)


def _check_timeline_item(
    check: dict[str, Any], head_sha: str, cfg: CodeRabbitConfig
) -> tuple[str, int, str, str, str, str] | None:
    if not _check_belongs(check, head_sha, cfg):
        return None
    status = str(check.get("status") or "").strip().lower()
    observed_at = str(check.get("completed_at") or check.get("started_at") or "")
    if status in CHECK_ACTIVE_STATUSES:
        return (observed_at, _entry_id(check), "check_run", "in_progress", KIND_IN_PROGRESS, "")
    if status and status != "completed":
        return None
    conclusion = str(check.get("conclusion") or "").strip().lower()
    kind = _map_check_conclusion(conclusion)
    return (observed_at, _entry_id(check), "check_run", conclusion, kind.value, "")


def _status_timeline_item(
    status: dict[str, Any], head_sha: str, cfg: CodeRabbitConfig
) -> tuple[str, int, str, str, str, str] | None:
    if not _status_belongs(status, head_sha, cfg):
        return None
    observed_at = str(status.get("updated_at") or status.get("created_at") or "")
    description = str(status.get("description") or "")
    state = str(status.get("state") or "").strip().lower()
    kind = _map_status_entry(state, description)
    if kind is None:
        return None
    conclusion = description.strip() or state
    return (
        observed_at,
        _entry_id(status),
        "commit_status",
        conclusion,
        kind.value,
        description.strip(),
    )


def _entry_id(payload: dict[str, Any]) -> int:
    raw = payload.get("id")
    if isinstance(raw, bool) or raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return 0


def _map_check_conclusion(conclusion: str) -> CodeRabbitTerminalKind:
    if conclusion == CHECK_SUCCESS:
        return CodeRabbitTerminalKind.COMPLETED
    if conclusion in CHECK_SKIPPED_CONCLUSIONS:
        return CodeRabbitTerminalKind.SKIPPED
    if conclusion in CHECK_FAILED_CONCLUSIONS:
        return CodeRabbitTerminalKind.FAILED
    return CodeRabbitTerminalKind.FAILED


def _map_status_entry(state: str, description: str) -> CodeRabbitTerminalKind | None:
    if state in STATUS_FAILED_STATES:
        return CodeRabbitTerminalKind.FAILED
    described = _kind_from_status_description(description)
    if described is not None:
        return described
    if state == STATUS_PENDING:
        return CodeRabbitTerminalKind.IN_PROGRESS
    if state == STATUS_SUCCESS:
        return CodeRabbitTerminalKind.AMBIGUOUS
    return None


def _kind_from_status_description(description: str) -> CodeRabbitTerminalKind | None:
    text = " ".join(description.strip().lower().split())
    if not text:
        return None
    if STATUS_DESC_SKIPPED in text:
        return CodeRabbitTerminalKind.SKIPPED
    if STATUS_DESC_IN_PROGRESS in text:
        return CodeRabbitTerminalKind.IN_PROGRESS
    if STATUS_DESC_COMPLETED in text:
        return CodeRabbitTerminalKind.COMPLETED
    return None
