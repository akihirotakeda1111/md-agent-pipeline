from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness.adapters import ReviewEvent, ReviewRunRequest, ServiceBundle
from .harness.git_repo import GitRepo

PR_NUMBER = 42
WORK_UNIT_ID = "phase7-integration"
TARGET_BRANCH = "agent/phase7-integration"
BOT = "coderabbitai[bot]"


def fixture(name: str) -> Any:
    path = Path(__file__).parent.parent / "fixtures" / "github" / name
    return json.loads(path.read_text(encoding="utf-8"))


def current_feedback(repo: GitRepo, name: str = "actionable-current.json") -> dict[str, Any]:
    value = fixture(name)
    value["head_sha"] = repo.head
    return value


def processed_record(feedback: dict[str, Any]) -> dict[str, Any]:
    import hashlib

    return {
        "object_id": str(feedback["id"]),
        "updated_at": str(feedback["updated_at"]),
        "body_sha256": hashlib.sha256(str(feedback["body"]).encode("utf-8")).hexdigest(),
        "work_unit_id": WORK_UNIT_ID,
        "pr_number": PR_NUMBER,
        "head_sha": str(feedback["head_sha"]),
    }


def request(spec_path: Path, repo: GitRepo, **event_overrides: Any) -> ReviewRunRequest:
    event_data = {
        "event_name": "pull_request_review_comment",
        "actor": BOT,
        "pr_number": PR_NUMBER,
        "head_sha": repo.head,
        "object_id": "comment-100",
    }
    event_data.update(event_overrides)
    return ReviewRunRequest(
        event=ReviewEvent(**event_data),
        spec_path=spec_path,
        repo_root=repo.root,
        work_unit_id=WORK_UNIT_ID,
        expected_pr_number=PR_NUMBER,
        expected_head_sha=repo.head,
        review_attempts=0,
        environment={
            "CODEX_API_KEY": "codex-test-key",
            "REVIEW_CLASSIFIER_API_KEY": "classifier-test-key",
            "GITHUB_TOKEN": "github-write-test-token",
            "GITHUB_REPOSITORY": "example/phase7",
        },
    )


def github_responses(
    repo: GitRepo, feedback: list[dict[str, Any]], **extra: Any
) -> dict[str, list[Any]]:
    responses: dict[str, list[Any]] = {
        "get_pull_request": [
            {
                "number": PR_NUMBER,
                "state": "open",
                "head": {"sha": repo.head, "ref": TARGET_BRANCH},
                "base": {"ref": "main"},
                "work_unit_id": WORK_UNIT_ID,
            }
        ],
        "list_review_feedback": [feedback],
        "load_processed_reviews": [[]],
        "save_processed_reviews": [{}],
        "list_check_runs": [[]],
        "list_commit_statuses": [[]],
        "list_pulls_for_commit": [[]],
    }
    responses.update(extra)
    return responses


def coderabbit_completed(repo: GitRepo, *, conclusion: str = "success") -> dict[str, list[Any]]:
    return {
        "list_check_runs": [
            [
                {
                    "head_sha": repo.head,
                    "status": "completed",
                    "conclusion": conclusion,
                    "completed_at": "2026-08-20T00:00:00Z",
                    "app": {"slug": "coderabbitai"},
                }
            ]
        ],
        "list_commit_statuses": [[]],
    }


def coderabbit_skipped(repo: GitRepo) -> dict[str, list[Any]]:
    return coderabbit_completed(repo, conclusion="skipped")


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    commit_count: int
    remote_head: str | None


def snapshot(repo: GitRepo) -> GitSnapshot:
    value = GitSnapshot(repo.head, repo.commit_count, repo.remote_ref())
    repo.observations.processes.clear()
    repo.observations.timeline.clear()
    return value


def assert_no_git_write(before: GitSnapshot, repo: GitRepo) -> None:
    assert repo.head == before.head
    assert repo.commit_count == before.commit_count
    assert repo.remote_ref() == before.remote_head


def assert_linear_push(before: GitSnapshot, repo: GitRepo) -> None:
    assert repo.commit_count == before.commit_count + 1
    assert repo.git("rev-parse", "HEAD^").stdout.strip() == before.head
    assert repo.remote_ref() == repo.head


def assert_no_codex(services: ServiceBundle) -> None:
    assert services.codex.invocations == []
