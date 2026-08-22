"""Wake-up gate for CodeRabbit GitHub events. Payload is not the review source of truth."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.config import AgentConfig, load_config
from agent.errors import AgentError
from agent.github_api import GitHubClient, github_client_from_env
from agent.pr import assert_spec_matches_marker, is_same_work_unit_pull, parse_work_unit_marker
from agent.review_collect import head_repo_full_name, head_sha_from_pull
from agent.review_terminal import (
    event_commit_sha,
    has_coderabbit_event_identity,
    is_terminal_wakeup_event,
)
from agent.spec import (
    TaskSpec,
    bind_spec_identity,
    canonicalize_spec_path,
    parse_spec,
    parse_spec_text,
)


@dataclass(frozen=True)
class ReviewPrepareResult:
    should_review: bool
    pull_number: int
    head_sha: str
    spec_id: str
    spec_path: str
    reason: str
    coderabbit_actor: str

    def to_output_map(self) -> dict[str, str]:
        return {
            "should_review": "true" if self.should_review else "false",
            "pull_number": str(self.pull_number),
            "head_sha": self.head_sha,
            "spec_id": self.spec_id,
            "spec_path": self.spec_path,
            "reason": self.reason,
            "coderabbit_actor": self.coderabbit_actor,
        }

    def to_json_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "should_review": self.should_review,
            "pull_number": self.pull_number,
            "head_sha": self.head_sha,
            "spec_id": self.spec_id,
            "spec_path": self.spec_path,
            "reason": self.reason,
            "coderabbit_actor": self.coderabbit_actor,
        }


def load_event_payload(path: Path | str) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentError.invalid_input("GITHUB_EVENT_PATH is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AgentError.invalid_input("GitHub event payload must be an object")
    return payload


def pull_number_from_event(payload: dict[str, Any]) -> int | None:
    pull = payload.get("pull_request")
    if isinstance(pull, dict) and isinstance(pull.get("number"), int):
        return pull["number"]
    issue = payload.get("issue")
    if (
        isinstance(issue, dict)
        and isinstance(issue.get("pull_request"), dict)
        and isinstance(issue.get("number"), int)
    ):
        return issue["number"]
    check_run = payload.get("check_run")
    if isinstance(check_run, dict):
        numbers = _unique_pull_numbers(check_run.get("pull_requests"))
        if len(numbers) == 1:
            return numbers[0]
    return None


def sender_login(payload: dict[str, Any]) -> str | None:
    sender = payload.get("sender")
    if isinstance(sender, dict):
        login = sender.get("login")
        if isinstance(login, str) and login.strip():
            return login
    return None


def find_spec_by_id(
    repo_root: Path | str,
    spec_id: str,
    *,
    config: AgentConfig | None = None,
) -> Path:
    cfg = config or load_config()
    directory = Path(repo_root) / cfg.task_spec.directory
    if not directory.is_dir():
        raise AgentError.environment_failure(
            f"task spec directory not found: {directory}",
            code="SPEC_NOT_FOUND",
        )
    matches: list[Path] = []
    for path in sorted(directory.rglob("*.md")):
        try:
            spec = parse_spec(path)
        except AgentError:
            continue
        if spec.id == spec_id:
            matches.append(path)
    if not matches:
        raise AgentError.escalation_required(
            f"no Task Spec found for spec_id {spec_id!r}",
            code="SPEC_NOT_FOUND",
        )
    if len(matches) > 1:
        raise AgentError.escalation_required(
            f"duplicate Task Spec id {spec_id!r}",
            code="DUPLICATE_SPEC_ID",
        )
    return matches[0]


def load_spec_from_marker_at_ref(
    client: GitHubClient,
    marker: dict[str, str],
    *,
    ref: str,
    repo_root: Path | str,
    spec_directory: str,
) -> tuple[str, TaskSpec]:
    """Load the Task Spec at marker.spec_path from current HEAD. No spec_id search."""
    marker_path = marker.get("spec_path") or ""
    if not marker_path or not marker.get("spec_sha256"):
        raise AgentError.escalation_required(
            "PR marker is missing spec_path/spec_sha256",
            code="SPEC_IDENTITY_MISMATCH",
        )
    canonical = canonicalize_spec_path(
        marker_path,
        repo_root=repo_root,
        spec_directory=spec_directory,
    )
    if canonical != marker_path:
        raise AgentError.escalation_required(
            f"PR marker spec_path is not canonical: {marker_path!r}",
            code="SPEC_IDENTITY_MISMATCH",
        )
    try:
        text = client.get_content(canonical, ref=ref)
    except AgentError as exc:
        if exc.code == "GITHUB_NOT_FOUND":
            raise AgentError.escalation_required(
                f"Task Spec not found at {canonical} ({ref})",
                code="SPEC_NOT_FOUND",
            ) from exc
        raise
    spec = parse_spec_text(text, source_path=canonical)
    spec = bind_spec_identity(spec, repo_root=repo_root, spec_directory=spec_directory)
    assert_spec_matches_marker(spec, marker)
    return canonical, spec


def find_spec_by_id_at_ref(
    client: GitHubClient,
    spec_id: str,
    *,
    ref: str,
    spec_directory: str,
) -> tuple[str, TaskSpec]:
    """Resolve a Task Spec from GitHub at an exact commit SHA. Ignores the checkout tree."""
    matches: list[tuple[str, TaskSpec]] = []
    for path in _markdown_paths_at_ref(client, spec_directory, ref):
        text = client.get_content(path, ref=ref)
        try:
            spec = parse_spec_text(text, source_path=path)
        except AgentError:
            continue
        if spec.id == spec_id:
            matches.append((path, spec))
    if not matches:
        raise AgentError.escalation_required(
            f"no Task Spec found for spec_id {spec_id!r} at {ref}",
            code="SPEC_NOT_FOUND",
        )
    if len(matches) > 1:
        raise AgentError.escalation_required(
            f"duplicate Task Spec id {spec_id!r} at {ref}",
            code="DUPLICATE_SPEC_ID",
        )
    return matches[0]


def _markdown_paths_at_ref(client: GitHubClient, directory: str, ref: str) -> list[str]:
    try:
        entries = client.list_contents(directory, ref=ref)
    except AgentError as exc:
        if exc.code == "GITHUB_NOT_FOUND":
            raise AgentError.escalation_required(
                f"task spec directory not found at {ref}: {directory}",
                code="SPEC_NOT_FOUND",
            ) from exc
        raise
    paths: list[str] = []
    for entry in entries:
        entry_path = str(entry.get("path") or "").replace("\\", "/")
        entry_type = str(entry.get("type") or "")
        if not entry_path:
            continue
        if entry_type == "dir":
            paths.extend(_markdown_paths_at_ref(client, entry_path, ref))
        elif entry_type == "file" and entry_path.endswith(".md"):
            paths.append(entry_path)
    return paths


def prepare_review(
    *,
    repo_root: Path | str,
    event_payload: dict[str, Any],
    repository: str,
    github: GitHubClient | None = None,
    config: AgentConfig | None = None,
) -> ReviewPrepareResult:
    cfg = config or load_config()
    client = github or github_client_from_env()
    actor = cfg.coderabbit.actor

    def skip(**kwargs: Any) -> ReviewPrepareResult:
        return _skip(coderabbit_actor=actor, **kwargs)

    if not is_terminal_wakeup_event(event_payload):
        number = pull_number_from_event(event_payload)
        return skip(
            pull_number=number or 0,
            reason="event is not a CodeRabbit terminal wake-up",
        )
    if not has_coderabbit_event_identity(event_payload, cfg.coderabbit):
        number = pull_number_from_event(event_payload)
        return skip(
            pull_number=number or 0,
            reason="event actor is not the configured CodeRabbit actor",
        )
    number = pull_number_from_event(event_payload)
    if number is None:
        number = _resolve_pull_number_from_sha(client, event_payload)
    if number is None:
        return skip(reason="event is not attached to a pull request")
    pull = client.get_pull(number)
    api_number = pull.get("number")
    if not isinstance(api_number, int) or isinstance(api_number, bool) or api_number != number:
        return skip(
            pull_number=number,
            reason="pull request identity does not match the wake-up event",
        )
    if str(pull.get("state") or "") != "open":
        return skip(pull_number=number, reason="pull request is not open")
    head_repo = head_repo_full_name(pull)
    if head_repo is None:
        return skip(pull_number=number, reason="pull request head repository is missing")
    if head_repo != repository:
        return skip(pull_number=number, reason="fork pull requests are not reviewed")
    head_sha = head_sha_from_pull(pull)
    if not head_sha:
        raise AgentError.environment_failure(
            "pull request head sha is missing",
            code="GITHUB_API_FAILURE",
        )
    event_sha = event_commit_sha(event_payload)
    if not _same_commit_sha(event_sha, head_sha):
        return skip(
            pull_number=number,
            head_sha=head_sha,
            reason="event sha is not the current pull head",
        )
    marker = parse_work_unit_marker(str(pull.get("body") or ""))
    if marker is None:
        return skip(
            pull_number=number,
            head_sha=head_sha,
            reason="pull request is not an orchestrator work unit",
        )
    spec_path, spec = load_spec_from_marker_at_ref(
        client,
        marker,
        ref=head_sha,
        repo_root=repo_root,
        spec_directory=cfg.task_spec.directory,
    )
    if not is_same_work_unit_pull(spec, pull):
        return skip(
            pull_number=number,
            head_sha=head_sha,
            spec_id=spec.id,
            spec_path=spec_path,
            reason="pull request does not match the work-unit marker",
        )
    return ReviewPrepareResult(
        should_review=True,
        pull_number=number,
        head_sha=head_sha,
        spec_id=spec.id,
        spec_path=spec_path,
        reason="ok",
        coderabbit_actor=actor,
    )


def _same_commit_sha(left: str, right: str) -> bool:
    first = left.strip().lower()
    second = right.strip().lower()
    return bool(first) and first == second


def _unique_pull_numbers(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            numbers.append(number)
    return sorted(set(numbers))


def _resolve_pull_number_from_sha(client: GitHubClient, payload: dict[str, Any]) -> int | None:
    sha = event_commit_sha(payload)
    if not sha:
        return None
    check_run = payload.get("check_run")
    if (
        isinstance(check_run, dict)
        and len(_unique_pull_numbers(check_run.get("pull_requests"))) > 1
    ):
        return None
    open_pulls = [
        item
        for item in client.list_pulls_for_commit(sha)
        if str(item.get("state") or "") == "open" and isinstance(item.get("number"), int)
    ]
    unique = sorted({int(item["number"]) for item in open_pulls})
    if len(unique) != 1:
        return None
    return unique[0]


def _skip(
    *,
    pull_number: int = 0,
    head_sha: str = "",
    spec_id: str = "",
    spec_path: str = "",
    reason: str,
    coderabbit_actor: str,
) -> ReviewPrepareResult:
    return ReviewPrepareResult(
        should_review=False,
        pull_number=pull_number,
        head_sha=head_sha,
        spec_id=spec_id,
        spec_path=spec_path,
        reason=reason,
        coderabbit_actor=coderabbit_actor,
    )
