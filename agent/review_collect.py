"""Re-fetch current Pull Request review feedback. Event payloads are wake-ups only."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from agent.github_api import GitHubClient
from agent.pr import WORK_UNIT_MARKER_START
from agent.review_filter import feedback_identity
from agent.review_track import REVIEW_STATE_START
from agent.review_types import ReviewFeedback

KIND_REVIEW = "pull_request_review"
KIND_REVIEW_COMMENT = "pull_request_review_comment"
KIND_ISSUE_COMMENT = "issue_comment"


def collect_review_feedback(
    client: GitHubClient,
    pull_number: int,
    *,
    actor: str,
) -> tuple[ReviewFeedback, ...]:
    items: list[ReviewFeedback] = []
    for review in client.list_reviews(pull_number):
        item = _from_review(review)
        if item is not None and item.author == actor:
            items.append(item)
    for comment in client.list_review_comments(pull_number):
        item = _from_review_comment(comment)
        if item is not None and item.author == actor:
            items.append(item)
    for comment in client.list_issue_comments(pull_number):
        item = _from_issue_comment(comment)
        if item is not None and item.author == actor:
            items.append(item)
    items.sort(key=lambda item: item.identity)
    return tuple(items)


def head_sha_from_pull(pull: Mapping[str, Any]) -> str:
    head = pull.get("head")
    if not isinstance(head, dict):
        return ""
    sha = head.get("sha")
    if not isinstance(sha, str) or not sha.strip():
        return ""
    return sha.strip()


def head_repo_full_name(pull: Mapping[str, Any]) -> str | None:
    head = pull.get("head")
    if not isinstance(head, dict):
        return None
    repo = head.get("repo")
    if not isinstance(repo, dict):
        return None
    name = repo.get("full_name")
    if not isinstance(name, str) or not name.strip():
        return None
    return name.strip()


def _from_review(payload: Mapping[str, Any]) -> ReviewFeedback | None:
    source_id = _int_id(payload.get("id"))
    body = _text(payload.get("body"))
    if source_id is None or _is_orchestrator_owned(body) or not body.strip():
        return None
    updated = _text(payload.get("submitted_at"))
    return ReviewFeedback(
        kind=KIND_REVIEW,
        identity=_identity(KIND_REVIEW, source_id, updated, body),
        source_id=source_id,
        updated_at=updated,
        author=_login(payload.get("user")),
        body=body,
        path=None,
        commit_sha=_optional_text(payload.get("commit_id")),
        html_url=_optional_text(payload.get("html_url")),
    )


def _from_review_comment(payload: Mapping[str, Any]) -> ReviewFeedback | None:
    source_id = _int_id(payload.get("id"))
    body = _text(payload.get("body"))
    if source_id is None or _is_orchestrator_owned(body) or not body.strip():
        return None
    updated = _text(payload.get("updated_at") or payload.get("created_at"))
    return ReviewFeedback(
        kind=KIND_REVIEW_COMMENT,
        identity=_identity(KIND_REVIEW_COMMENT, source_id, updated, body),
        source_id=source_id,
        updated_at=updated,
        author=_login(payload.get("user")),
        body=body,
        path=_optional_text(payload.get("path")),
        commit_sha=_optional_text(payload.get("commit_id") or payload.get("original_commit_id")),
        html_url=_optional_text(payload.get("html_url")),
    )


def _from_issue_comment(payload: Mapping[str, Any]) -> ReviewFeedback | None:
    source_id = _int_id(payload.get("id"))
    body = _text(payload.get("body"))
    if source_id is None or _is_orchestrator_owned(body) or not body.strip():
        return None
    updated = _text(payload.get("updated_at") or payload.get("created_at"))
    return ReviewFeedback(
        kind=KIND_ISSUE_COMMENT,
        identity=_identity(KIND_ISSUE_COMMENT, source_id, updated, body),
        source_id=source_id,
        updated_at=updated,
        author=_login(payload.get("user")),
        body=body,
        path=None,
        commit_sha=None,
        html_url=_optional_text(payload.get("html_url")),
    )


def _identity(kind: str, source_id: int, updated_at: str, body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    return f"{feedback_identity(kind, source_id, updated_at)}:{digest}"


def _is_orchestrator_owned(body: str) -> bool:
    return REVIEW_STATE_START in body or WORK_UNIT_MARKER_START in body


def _login(user: Any) -> str:
    if isinstance(user, dict):
        login = user.get("login")
        if isinstance(login, str):
            return login
    return ""


def _int_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None
