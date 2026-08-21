"""Thin Production binding for the Phase 7 integration suite.

Translates test DTOs and injects Fake GitHub / classifier / Codex. Does not
implement review policy, scope, validation, convergence, or Git decisions.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent.codex_runner import ProcessResult
from agent.config import load_config
from agent.errors import AgentError, ErrorCategory
from agent.pr import WORK_UNIT_MARKER_END, WORK_UNIT_MARKER_START
from agent.review_classify import classify_review_comment, load_classification_schema
from agent.review_collect import KIND_ISSUE_COMMENT, KIND_REVIEW, KIND_REVIEW_COMMENT
from agent.review_filter import feedback_identity
from agent.review_loop import ReviewResult, run_review
from agent.review_prepare import prepare_review
from agent.review_track import (
    REVIEW_STATE_START,
    ReviewTrack,
    empty_review_track,
    parse_review_track,
    render_review_track,
)
from agent.spec import parse_spec

from .common import BOT
from .harness.adapters import ReviewRunRequest, ReviewRunResult, ServiceBundle
from .harness.fake_github import FakeGitHub
from .harness.observations import ObservationLog

_JOB_CREDENTIALS = (
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "REVIEW_CLASSIFIER_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_PAT",
    "AGENT_PR_PAT",
)

_KIND_MAP = {
    "review": KIND_REVIEW,
    "review_comment": KIND_REVIEW_COMMENT,
    "issue_comment": KIND_ISSUE_COMMENT,
    KIND_REVIEW: KIND_REVIEW,
    KIND_REVIEW_COMMENT: KIND_REVIEW_COMMENT,
    KIND_ISSUE_COMMENT: KIND_ISSUE_COMMENT,
}


class _FakeGitHubClient:
    """Maps Production GitHubClient methods onto the recording FakeGitHub."""

    def __init__(
        self, fake: FakeGitHub, *, repository: str, track_author: str, repo_root: Path
    ) -> None:
        self._fake = fake
        self._repository = repository
        self._track_author = track_author
        self._repo_root = Path(repo_root)
        self._feedback_cache: list[dict[str, Any]] | None = None

    def _feedback(self) -> list[dict[str, Any]]:
        if self._feedback_cache is None:
            payload = self._fake.list_review_feedback()
            self._feedback_cache = payload if isinstance(payload, list) else []
        return self._feedback_cache

    def get_pull(self, number: int) -> dict[str, Any]:
        payload = self._fake.get_pull_request(number=number)
        raw = payload if isinstance(payload, dict) else {}
        return _production_pull(raw, repository=self._repository, requested_number=number)

    def get_content(self, path: str, *, ref: str) -> str:
        self._fake.request("get_content", path=path, ref=ref)
        return _contained_path(self._repo_root, path).read_text(encoding="utf-8")

    def list_contents(self, path: str, *, ref: str) -> list[dict[str, Any]]:
        self._fake.request("list_contents", path=path, ref=ref)
        directory = _contained_path(self._repo_root, path)
        if not directory.is_dir():
            raise AgentError.environment_failure(
                f"GitHub content is not a directory: {path}",
                code="GITHUB_NOT_FOUND",
            )
        entries: list[dict[str, Any]] = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = child.relative_to(self._repo_root).as_posix()
            entries.append(
                {
                    "type": "dir" if child.is_dir() else "file",
                    "path": relative,
                    "name": child.name,
                }
            )
        return entries

    def list_reviews(self, pull_number: int) -> list[dict[str, Any]]:
        return [_as_github_review(item) for item in self._feedback() if _kind(item) == KIND_REVIEW]

    def list_review_comments(self, pull_number: int) -> list[dict[str, Any]]:
        return [
            _as_github_review_comment(item)
            for item in self._feedback()
            if _kind(item) == KIND_REVIEW_COMMENT
        ]

    def list_check_runs_for_ref(self, ref: str) -> list[dict[str, Any]]:
        payload = self._fake.request("list_check_runs", ref=ref)
        if isinstance(payload, dict):
            runs = payload.get("check_runs")
            return [item for item in runs if isinstance(item, dict)] if isinstance(runs, list) else []
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def list_commit_statuses_for_ref(self, ref: str) -> list[dict[str, Any]]:
        payload = self._fake.request("list_commit_statuses", ref=ref)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def list_pulls_for_commit(self, sha: str) -> list[dict[str, Any]]:
        payload = self._fake.request("list_pulls_for_commit", sha=sha)
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    def list_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        comments = [
            _as_github_issue_comment(item)
            for item in self._feedback()
            if _kind(item) == KIND_ISSUE_COMMENT
        ]
        tracking = self._tracking_comment()
        if tracking is not None:
            comments.append(tracking)
        comments.extend(dict(item) for item in self._fake.issue_comments)
        return comments

    def create_issue_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        if REVIEW_STATE_START in body:
            saved = self._fake.save_processed_reviews(
                issue_number=issue_number,
                body=body,
                records=_records_from_track_body(body),
            )
            comment_id = saved.get("id") if isinstance(saved, dict) else None
            if not isinstance(comment_id, int):
                comment_id = self._fake.tracking_id or self._fake.next_id
            return {
                "id": comment_id,
                "body": body,
                "user": {"login": self._track_author},
            }
        return self._fake.add_pr_comment(
            issue_number=issue_number,
            body=body,
            user={"login": self._track_author},
        )

    def update_issue_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        saved = self._fake.save_processed_reviews(
            comment_id=comment_id,
            body=body,
            records=_records_from_track_body(body),
        )
        payload = saved if isinstance(saved, dict) else {}
        payload.setdefault("id", comment_id)
        payload["body"] = body
        payload.setdefault("user", {"login": self._track_author})
        self._fake.tracking_id = comment_id
        self._fake.tracking_body = body
        return payload

    def get_label(self, name: str) -> dict[str, Any] | None:
        self._fake.request("get_label", name=name)
        if name in self._fake.labels:
            return {"name": name}
        return None

    def create_label(self, *, name: str, color: str, description: str) -> dict[str, Any]:
        self._fake.set_labels(name=name, color=color, description=description, labels=[name])
        return {"name": name, "color": color, "description": description}

    def add_issue_labels(self, issue_number: int, labels: list[str]) -> None:
        self._fake.set_labels(issue_number=issue_number, labels=labels)

    def remove_issue_label(self, issue_number: int, name: str) -> None:
        self._fake.request("remove_label", issue_number=issue_number, name=name)
        self._fake.labels.discard(name)

    def _tracking_comment(self) -> dict[str, Any] | None:
        if self._fake.tracking_body:
            return {
                "id": self._fake.tracking_id or 1,
                "body": self._fake.tracking_body,
                "user": {"login": self._track_author},
            }
        return None


def _contained_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise AgentError.invalid_input(f"invalid repository content path: {relative!r}") from exc
    return candidate


def _kind(item: dict[str, Any]) -> str:
    return _KIND_MAP.get(str(item.get("kind") or "review_comment"), KIND_REVIEW_COMMENT)


def _source_id(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if digits:
        return int(digits)
    return abs(hash(str(value))) % 10_000_000


def _login(item: dict[str, Any]) -> str:
    user = item.get("user")
    if isinstance(user, dict) and isinstance(user.get("login"), str):
        return user["login"]
    actor = item.get("actor")
    return actor if isinstance(actor, str) else ""


def _commit_sha(item: dict[str, Any]) -> str | None:
    for key in ("commit_id", "head_sha", "commit_sha"):
        value = item.get(key)
        if isinstance(value, str) and value.strip() and value != "__CURRENT_HEAD__":
            return value.strip()
    return None


def _as_github_review(item: dict[str, Any]) -> dict[str, Any]:
    updated = str(
        item.get("updated_at") or item.get("submitted_at") or item.get("created_at") or ""
    )
    return {
        "id": _source_id(item.get("id")),
        "body": str(item.get("body") or ""),
        "user": {"login": _login(item)},
        "submitted_at": updated,
        "commit_id": _commit_sha(item),
        "html_url": item.get("html_url"),
    }


def _as_github_review_comment(item: dict[str, Any]) -> dict[str, Any]:
    updated = str(item.get("updated_at") or item.get("created_at") or "")
    return {
        "id": _source_id(item.get("id")),
        "body": str(item.get("body") or ""),
        "user": {"login": _login(item)},
        "path": item.get("path"),
        "updated_at": updated,
        "created_at": str(item.get("created_at") or updated),
        "commit_id": _commit_sha(item),
        "html_url": item.get("html_url"),
    }


def _as_github_issue_comment(item: dict[str, Any]) -> dict[str, Any]:
    updated = str(item.get("updated_at") or item.get("created_at") or "")
    return {
        "id": _source_id(item.get("id")),
        "body": str(item.get("body") or ""),
        "user": {"login": _login(item)},
        "updated_at": updated,
        "created_at": str(item.get("created_at") or updated),
        "html_url": item.get("html_url"),
    }


def _production_pull(
    raw: dict[str, Any], *, repository: str, requested_number: int
) -> dict[str, Any]:
    pull = dict(raw)
    pull.setdefault("number", requested_number)
    pull.setdefault("state", "open")
    head = dict(pull.get("head") or {})
    repo = dict(head.get("repo") or {})
    repo.setdefault("full_name", repository)
    head["repo"] = repo
    pull["head"] = head
    base = dict(pull.get("base") or {})
    pull["base"] = base
    if not str(pull.get("body") or "").strip():
        spec_id = str(pull.get("work_unit_id") or "")
        pull["body"] = _marker(
            spec_id=spec_id,
            base_branch=str(base.get("ref") or "main"),
            target_branch=str(head.get("ref") or ""),
        )
    return pull


def _marker(*, spec_id: str, base_branch: str, target_branch: str) -> str:
    return "\n".join(
        [
            WORK_UNIT_MARKER_START,
            f"spec_id: {spec_id}",
            f"base_branch: {base_branch}",
            f"target_branch: {target_branch}",
            WORK_UNIT_MARKER_END,
        ]
    )


def _records_from_track_body(body: str) -> list[str]:
    parsed = parse_review_track(body)
    if parsed is None:
        return []
    return list(parsed.processed)


def _identity_from_record(record: dict[str, Any], feedback: list[dict[str, Any]]) -> str:
    object_id = record.get("object_id")
    source_id = _source_id(object_id)
    updated_at = str(record.get("updated_at") or "")
    digest = str(record.get("body_sha256") or "")[:12]
    if not digest:
        digest = hashlib.sha256(str(record.get("body") or "").encode("utf-8")).hexdigest()[:12]
    matched = next(
        (
            item
            for item in feedback
            if str(item.get("id")) == str(object_id) or _source_id(item.get("id")) == source_id
        ),
        None,
    )
    kind = _kind(matched) if matched is not None else KIND_REVIEW_COMMENT
    return f"{feedback_identity(kind, source_id, updated_at)}:{digest}"


def _seed_tracking(fake: FakeGitHub, *, spec, request: ReviewRunRequest) -> None:
    if fake.tracking_body:
        return
    records = fake.current("load_processed_reviews", [])
    identities: list[str] = []
    if isinstance(records, list):
        feedback = fake.current("list_review_feedback", [])
        items = feedback if isinstance(feedback, list) else []
        for record in records:
            if isinstance(record, str) and record.strip():
                identities.append(record)
            elif isinstance(record, dict):
                identities.append(_identity_from_record(record, items))
    if not identities and request.review_attempts <= 0:
        return
    base = empty_review_track(spec)
    track = ReviewTrack(
        spec_id=base.spec_id,
        base_branch=base.base_branch,
        target_branch=base.target_branch,
        review_attempts=request.review_attempts,
        processed=tuple(identities),
        head_sha="",
        schema_version=base.schema_version,
    )
    fake.tracking_body = render_review_track(track)
    fake.tracking_id = fake.next_id
    fake.next_id += 1


def _event_payload(request: ReviewRunRequest) -> dict[str, Any]:
    actor = request.event.actor
    slug = "coderabbitai" if actor == BOT else "github-actions"
    return {
        "sender": {"login": actor},
        "check_run": {
            "head_sha": request.event.head_sha,
            "status": "completed",
            "conclusion": "success",
            "app": {"slug": slug},
            "pull_requests": [{"number": request.event.pr_number}],
        },
    }


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


def _codex_executor(services: ServiceBundle):
    def executor(
        command: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        timeout: int,
        stdin: str,
    ) -> ProcessResult:
        result = services.codex.invoke(
            workspace=Path(cwd),
            prompt=stdin,
            env=dict(env),
            argv=list(command),
        )
        message = str(result.get("final_message") or "")
        if "--output-last-message" in command:
            index = command.index("--output-last-message")
            Path(command[index + 1]).write_text(message, encoding="utf-8")
        return ProcessResult(returncode=int(result["exit_code"]), stdout=message, stderr="")

    return executor


def _classifier_fn(services: ServiceBundle, api_key: str):
    schema = load_classification_schema()
    env = {load_config().review.api_key_env: api_key}

    def classifier(item, spec):
        raw = services.classifier.classify(
            payload=item.to_json_dict(),
            schema=dict(schema),
            env=dict(env),
        )

        def requester(_body: dict[str, Any], _key: str) -> dict[str, Any]:
            return {"choices": [{"finish_reason": "stop", "message": {"content": raw}}]}

        return classify_review_comment(item, spec, api_key=api_key, requester=requester)

    return classifier


def _rel_spec_path(request: ReviewRunRequest) -> str:
    try:
        return request.spec_path.resolve().relative_to(request.repo_root.resolve()).as_posix()
    except ValueError:
        return request.spec_path.as_posix().replace("\\", "/")


def _result_from_prepare(*, status: str, reason: str, request: ReviewRunRequest) -> ReviewRunResult:
    return ReviewRunResult(
        status=status,
        reason=reason,
        review_attempts=request.review_attempts,
    )


def _result_from_review(result: ReviewResult, observations: ObservationLog) -> ReviewRunResult:
    return ReviewRunResult(
        status=result.outcome,
        reason=result.code or result.message,
        review_attempts=result.review_attempts,
        processed_identities=list(result.processed),
        events=list(observations.events),
        summary=result.message,
    )


class ProductionPhase7Driver:
    def run_review(self, request: ReviewRunRequest, services: ServiceBundle) -> ReviewRunResult:
        cfg = load_config()
        spec = parse_spec(request.spec_path)
        github = _FakeGitHubClient(
            services.github,
            repository=request.environment.get("GITHUB_REPOSITORY", ""),
            track_author=cfg.review.track_author,
            repo_root=request.repo_root,
        )
        _seed_tracking(services.github, spec=spec, request=request)
        classifier_key = request.environment.get(cfg.review.api_key_env, "")
        with (
            _isolated_job_environ(request.environment) as env,
            _capture_events(services.observations),
        ):
            try:
                prepared = prepare_review(
                    repo_root=request.repo_root,
                    event_payload=_event_payload(request),
                    repository=request.environment.get("GITHUB_REPOSITORY", ""),
                    github=github,  # type: ignore[arg-type]
                    config=cfg,
                )
            except AgentError as exc:
                status = (
                    "FAILED" if exc.category is ErrorCategory.ENVIRONMENT_FAILURE else "ESCALATED"
                )
                return _result_from_prepare(
                    status=status, reason=exc.code or str(exc), request=request
                )
            if not prepared.should_review:
                return _result_from_prepare(
                    status="SKIPPED", reason=prepared.reason, request=request
                )
            result = run_review(
                repo_root=request.repo_root,
                pull_number=prepared.pull_number,
                head_sha_expected=prepared.head_sha,
                spec_path=prepared.spec_path or _rel_spec_path(request),
                config=cfg,
                github=github,  # type: ignore[arg-type]
                classifier=_classifier_fn(services, classifier_key),
                executor=_codex_executor(services),
                env=env,
            )
        return _result_from_review(result, services.observations)


def create_driver() -> ProductionPhase7Driver:
    return ProductionPhase7Driver()
