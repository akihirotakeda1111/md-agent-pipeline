"""Map GitHub Checks / commit statuses to CodeRabbit terminal kinds.

Keep this aligned with `agent/review_terminal.py`. The E2E venv does not install
the production package, and live COMPLETED/SKIPPED payloads have not locked a
single transport yet, so the harness re-fetches both Checks and commit statuses.
Wake-up event payloads are not the source of truth.
"""

from __future__ import annotations

from typing import Any


KIND_COMPLETED = "CODERABBIT_COMPLETED"
KIND_SKIPPED = "CODERABBIT_SKIPPED"
KIND_FAILED = "CODERABBIT_FAILED"
KIND_IN_PROGRESS = "IN_PROGRESS"
KIND_NONE = "NONE"

CHECK_SKIPPED_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
CHECK_FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "stale", "error"}
)
CHECK_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})
STATUS_FAILED_STATES = frozenset({"failure", "error"})


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
    }
    if not expected:
        return empty
    matching: list[tuple[str, str, str, str]] = []
    active = False
    for check in check_runs:
        sha = str(check.get("head_sha") or "").strip()
        if sha and sha != expected:
            continue
        app = check.get("app")
        slug = str(app.get("slug") or "").strip() if isinstance(app, dict) else ""
        if slug != check_app_slug.strip():
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
                _map_check_conclusion(conclusion),
            )
        )
    for status in statuses:
        context = status.get("context")
        creator = status.get("creator")
        login = creator.get("login") if isinstance(creator, dict) else None
        context_ok = isinstance(context, str) and status_context_matches(context, status_context)
        actor_ok = isinstance(login, str) and login.strip() == actor.strip()
        if not context_ok and not actor_ok:
            continue
        state = str(status.get("state") or "").strip().lower()
        if state == "pending":
            active = True
            continue
        matching.append(
            (
                str(status.get("updated_at") or status.get("created_at") or ""),
                "commit_status",
                state,
                _map_status_state(state),
            )
        )
    if active:
        return {
            "kind": KIND_IN_PROGRESS,
            "source": "",
            "head_sha": expected,
            "conclusion": "in_progress",
            "observed_at": "",
        }
    if not matching:
        return empty
    matching.sort(key=lambda item: item[0])
    observed_at, source, conclusion, kind = matching[-1]
    return {
        "kind": kind,
        "source": source,
        "head_sha": expected,
        "conclusion": conclusion,
        "observed_at": observed_at,
    }


def _map_check_conclusion(conclusion: str) -> str:
    if conclusion == "success":
        return KIND_COMPLETED
    if conclusion in CHECK_SKIPPED_CONCLUSIONS:
        return KIND_SKIPPED
    return KIND_FAILED


def _map_status_state(state: str) -> str:
    if state == "success":
        return KIND_COMPLETED
    if state in STATUS_FAILED_STATES:
        return KIND_FAILED
    return KIND_FAILED
