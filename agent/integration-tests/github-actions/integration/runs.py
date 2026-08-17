"""Select the workflow run this harness just triggered."""

from __future__ import annotations

from typing import Any


def match_triggered_runs(
    items: list[dict[str, Any]],
    *,
    branch: str,
    event: str,
    head_sha: str,
    known_ids: set[int],
) -> list[dict[str, Any]]:
    """Return runs that match the trigger identity. SHA is required."""
    if not branch or not event or not head_sha:
        raise ValueError("branch, event, and head_sha are required to identify a run")
    matches: list[dict[str, Any]] = []
    for item in items:
        run_id = item.get("databaseId")
        if not isinstance(run_id, int):
            continue
        if run_id in known_ids:
            continue
        if item.get("event") != event:
            continue
        if item.get("headBranch") != branch:
            continue
        if item.get("headSha") != head_sha:
            continue
        matches.append(item)
    return matches


def require_unique_run(
    matches: list[dict[str, Any]], *, branch: str, event: str, head_sha: str
) -> int:
    if not matches:
        raise LookupError(f"no {event} run for {branch}@{head_sha}")
    if len(matches) > 1:
        ids = [item.get("databaseId") for item in matches]
        raise RuntimeError(f"ambiguous {event} runs for {branch}@{head_sha}: {ids}")
    run_id = matches[0].get("databaseId")
    if not isinstance(run_id, int):
        raise RuntimeError("matched run is missing databaseId")
    return run_id


def check_run_identity(
    *,
    expected_event: str,
    expected_branch: str,
    expected_sha: str,
    event: str | None,
    branch: str | None,
    sha: str | None,
) -> list[str]:
    errors: list[str] = []
    if event != expected_event:
        errors.append(f"event: expected {expected_event}, got {event}")
    if branch != expected_branch:
        errors.append(f"branch: expected {expected_branch}, got {branch}")
    if sha != expected_sha:
        errors.append(f"head_sha: expected {expected_sha}, got {sha}")
    return errors
