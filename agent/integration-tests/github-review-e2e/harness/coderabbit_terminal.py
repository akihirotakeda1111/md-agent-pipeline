"""Map GitHub Checks / commit statuses to CodeRabbit terminal kinds.

Keep this aligned with `agent/review_terminal.py`. The E2E venv does not install
the production package. Wake-up event payloads are not the source of truth.
On the current HEAD the latest matching Check or commit status wins. Commit
status `state=success` is COMPLETED only when the description is Review completed.
Missing or unknown descriptions are CODERABBIT_AMBIGUOUS and fail-closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

KIND_COMPLETED = "CODERABBIT_COMPLETED"
KIND_SKIPPED = "CODERABBIT_SKIPPED"
KIND_FAILED = "CODERABBIT_FAILED"
KIND_AMBIGUOUS = "CODERABBIT_AMBIGUOUS"
KIND_IN_PROGRESS = "IN_PROGRESS"
KIND_NONE = "NONE"

CHECK_SKIPPED_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
CHECK_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "action_required", "stale", "error"})
CHECK_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
STATUS_FAILED_STATES = frozenset({"failure", "error"})
STATUS_DESC_COMPLETED = "review completed"
STATUS_DESC_SKIPPED = "review skipped"
STATUS_DESC_IN_PROGRESS = "review in progress"


def status_context_matches(context: str, configured: str) -> bool:
    value = context.strip()
    expected = configured.strip()
    if not value or not expected:
        return False
    return value == expected or value.startswith(f"{expected}/") or value.startswith(f"{expected} ")


def resolve_coderabbit_terminal(
    check_runs: list[dict[str, Any]],
    statuses: list[dict[str, Any]],
    *,
    head_sha: str,
    actor: str,
    check_app_slug: str,
    status_context: str,
) -> dict[str, str]:
    expected = head_sha.strip()
    empty = {
        "kind": KIND_NONE,
        "source": "",
        "head_sha": expected,
        "conclusion": "",
        "observed_at": "",
        "description": "",
    }
    if not expected:
        return empty
    matching: list[tuple[str, int, str, str, str, str]] = []
    for check in check_runs:
        item = _check_timeline_item(check, expected, check_app_slug)
        if item is not None:
            matching.append(item)
    for status in statuses:
        item = _status_timeline_item(status, expected, actor, status_context)
        if item is not None:
            matching.append(item)
    if not matching:
        return empty
    matching.sort(key=lambda row: (row[0], row[1]))
    observed_at, _, source, conclusion, kind, description = matching[-1]
    if kind == KIND_NONE:
        return empty
    return {
        "kind": kind,
        "source": source,
        "head_sha": expected,
        "conclusion": conclusion,
        "observed_at": observed_at,
        "description": description,
    }


def _check_timeline_item(
    check: dict[str, Any], expected: str, check_app_slug: str
) -> tuple[str, int, str, str, str, str] | None:
    sha = str(check.get("head_sha") or "").strip()
    if sha and sha != expected:
        return None
    app = check.get("app")
    slug = str(app.get("slug") or "").strip() if isinstance(app, dict) else ""
    if slug != check_app_slug.strip():
        return None
    status = str(check.get("status") or "").strip().lower()
    observed_at = str(check.get("completed_at") or check.get("started_at") or "")
    if status in CHECK_ACTIVE_STATUSES:
        return (observed_at, _entry_id(check), "check_run", "in_progress", KIND_IN_PROGRESS, "")
    if status and status != "completed":
        return None
    conclusion = str(check.get("conclusion") or "").strip().lower()
    return (
        observed_at,
        _entry_id(check),
        "check_run",
        conclusion,
        _map_check_conclusion(conclusion),
        "",
    )


def _status_timeline_item(
    status: dict[str, Any], expected: str, actor: str, status_context: str
) -> tuple[str, int, str, str, str, str] | None:
    sha = str(status.get("sha") or "").strip()
    if sha and sha != expected:
        return None
    context = status.get("context")
    creator = status.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    context_ok = isinstance(context, str) and status_context_matches(context, status_context)
    actor_ok = isinstance(login, str) and login.strip() == actor.strip()
    if not context_ok and not actor_ok:
        return None
    observed_at = str(status.get("updated_at") or status.get("created_at") or "")
    description = str(status.get("description") or "").strip()
    state = str(status.get("state") or "").strip().lower()
    kind = _map_status_entry(state, description)
    if kind is None:
        return None
    return (
        observed_at,
        _entry_id(status),
        "commit_status",
        description or state,
        kind,
        description,
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


def _map_check_conclusion(conclusion: str) -> str:
    if conclusion == "success":
        return KIND_COMPLETED
    if conclusion in CHECK_SKIPPED_CONCLUSIONS:
        return KIND_SKIPPED
    if conclusion in CHECK_FAILED_CONCLUSIONS:
        return KIND_FAILED
    return KIND_FAILED


def _map_status_entry(state: str, description: str) -> str | None:
    if state in STATUS_FAILED_STATES:
        return KIND_FAILED
    described = _kind_from_status_description(description)
    if described is not None:
        return described
    if state == "pending":
        return KIND_IN_PROGRESS
    if state == "success":
        return KIND_AMBIGUOUS
    return None


def _kind_from_status_description(description: str) -> str | None:
    text = " ".join(description.strip().lower().split())
    if not text:
        return None
    if STATUS_DESC_SKIPPED in text:
        return KIND_SKIPPED
    if STATUS_DESC_IN_PROGRESS in text:
        return KIND_IN_PROGRESS
    if STATUS_DESC_COMPLETED in text:
        return KIND_COMPLETED
    return None


OBSERVED_CASES_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "coderabbit_terminal_cases.json"
)


def load_observed_cases() -> dict[str, Any]:
    return json.loads(OBSERVED_CASES_PATH.read_text(encoding="utf-8"))


def bind_observed_case(
    case: dict[str, Any], *, current: str = "abc", old: str = "old"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    mapping = {"current": current, "old": old}

    def bind_item(item: dict[str, Any], sha_key: str) -> dict[str, Any]:
        bound = dict(item)
        token = bound.get(sha_key)
        if token in mapping:
            bound[sha_key] = mapping[token]
        return bound

    checks = [bind_item(item, "head_sha") for item in case.get("check_runs") or []]
    statuses = [bind_item(item, "sha") for item in case.get("statuses") or []]
    return checks, statuses, current
