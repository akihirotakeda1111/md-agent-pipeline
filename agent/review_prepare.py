"""Wake-up gate for CodeRabbit GitHub events. Payload is not the review source of truth."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.config import AgentConfig, load_config
from agent.errors import AgentError
from agent.github_api import GitHubClient, github_client_from_env
from agent.pr import is_same_work_unit_pull, parse_work_unit_marker
from agent.review_collect import head_repo_full_name, head_sha_from_pull
from agent.review_filter import is_configured_actor
from agent.spec import parse_spec


@dataclass(frozen=True)
class ReviewPrepareResult:
    should_review: bool
    pull_number: int
    head_sha: str
    spec_id: str
    spec_path: str
    reason: str

    def to_output_map(self) -> dict[str, str]:
        return {
            "should_review": "true" if self.should_review else "false",
            "pull_number": str(self.pull_number),
            "head_sha": self.head_sha,
            "spec_id": self.spec_id,
            "spec_path": self.spec_path,
            "reason": self.reason,
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
    number = pull_number_from_event(event_payload)
    if number is None:
        return _skip(reason="event is not attached to a pull request")
    actor = sender_login(event_payload)
    if not is_configured_actor(actor, cfg.coderabbit.actor):
        return _skip(
            pull_number=number, reason="event actor is not the configured CodeRabbit actor"
        )
    pull = client.get_pull(number)
    if str(pull.get("state") or "") != "open":
        return _skip(pull_number=number, reason="pull request is not open")
    head_repo = head_repo_full_name(pull)
    if head_repo is None:
        return _skip(pull_number=number, reason="pull request head repository is missing")
    if head_repo != repository:
        return _skip(pull_number=number, reason="fork pull requests are not reviewed")
    head_sha = head_sha_from_pull(pull)
    if not head_sha:
        raise AgentError.environment_failure(
            "pull request head sha is missing",
            code="GITHUB_API_FAILURE",
        )
    marker = parse_work_unit_marker(str(pull.get("body") or ""))
    if marker is None:
        return _skip(
            pull_number=number,
            head_sha=head_sha,
            reason="pull request is not an orchestrator work unit",
        )
    spec_path = find_spec_by_id(repo_root, marker["spec_id"], config=cfg)
    spec = parse_spec(spec_path)
    if not is_same_work_unit_pull(spec, pull):
        return _skip(
            pull_number=number,
            head_sha=head_sha,
            spec_id=spec.id,
            spec_path=_rel(spec_path, repo_root),
            reason="pull request does not match the work-unit marker",
        )
    return ReviewPrepareResult(
        should_review=True,
        pull_number=number,
        head_sha=head_sha,
        spec_id=spec.id,
        spec_path=_rel(spec_path, repo_root),
        reason="ok",
    )


def _rel(path: Path, repo_root: Path | str) -> str:
    root = Path(repo_root).resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix().replace("\\", "/")


def _skip(
    *,
    pull_number: int = 0,
    head_sha: str = "",
    spec_id: str = "",
    spec_path: str = "",
    reason: str,
) -> ReviewPrepareResult:
    return ReviewPrepareResult(
        should_review=False,
        pull_number=pull_number,
        head_sha=head_sha,
        spec_id=spec_id,
        spec_path=spec_path,
        reason=reason,
    )
