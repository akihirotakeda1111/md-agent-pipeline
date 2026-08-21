"""Deterministic pre-filter for untrusted review comments. No LLM."""

from __future__ import annotations

from pathlib import Path

from agent.config import RuntimeEditPolicy
from agent.review_types import ReviewFeedback
from agent.scope import path_is_in_scope
from agent.spec import TaskSpec


def is_configured_actor(login: str | None, expected: str) -> bool:
    if login is None or not expected.strip():
        return False
    return login == expected


def feedback_identity(kind: str, source_id: int, updated_at: str) -> str:
    return f"{kind}:{source_id}:{updated_at}"


def is_processed(identity: str, processed: set[str]) -> bool:
    return identity in processed


def is_outdated(item: ReviewFeedback, head_sha: str) -> bool:
    if not item.commit_sha:
        return False
    return item.commit_sha != head_sha


def applies_to_current_head(item: ReviewFeedback, head_sha: str, track_head_sha: str) -> bool:
    """True when the comment is feedback on the current PR HEAD.

    Inline reviews carry commit_sha. Conversation comments without a commit
    apply only while the tracking record is already bound to this HEAD, or on
    the first evaluation (empty track head).
    """
    if item.commit_sha:
        return item.commit_sha == head_sha
    return not track_head_sha or track_head_sha == head_sha


def path_is_missing(item: ReviewFeedback, repo_root: Path | str) -> bool:
    if not item.path:
        return False
    return not (Path(repo_root) / item.path).exists()


def path_is_obviously_forbidden(
    item: ReviewFeedback, spec: TaskSpec, runtime_policy: RuntimeEditPolicy
) -> bool:
    if not item.path:
        return False
    return not path_is_in_scope(item.path, spec, runtime_policy)


def prefilter_reason(
    item: ReviewFeedback,
    *,
    spec: TaskSpec,
    runtime_policy: RuntimeEditPolicy,
    actor: str,
    head_sha: str,
    processed: set[str],
    repo_root: Path | str,
    track_head_sha: str = "",
) -> str | None:
    """Return a skip reason, or None when the item may go to the classifier."""
    if not is_configured_actor(item.author, actor):
        return "non-configured-actor"
    if is_processed(item.identity, processed):
        return "processed"
    if not applies_to_current_head(item, head_sha, track_head_sha):
        return "outdated-head"
    if path_is_missing(item, repo_root):
        return "missing-path"
    if path_is_obviously_forbidden(item, spec, runtime_policy):
        return "forbidden-path"
    return None
