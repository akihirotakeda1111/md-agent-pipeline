from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness.adapters import DeliveryRequest, DeliveryResult, ServiceBundle
from .harness.git_repo import GitRepo


TARGET_BRANCH = "agent/phase6-integration"

HAPPY_PATH_CHANGES = {
    "app/task-1.txt": "one\n",
    "app/task-2.txt": "two\n",
}

NEW_PR_GITHUB = {
    "list_pull_requests": [[]],
    "create_pull_request": [{"number": 1, "html_url": "https://example.invalid/pull/1"}],
}


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    commits: int
    remote_target: str | None


def snapshot(repo: GitRepo) -> GitSnapshot:
    value = GitSnapshot(repo.head, repo.commit_count, repo.remote_ref(TARGET_BRANCH))
    repo.observations.timeline.clear()
    repo.observations.processes.clear()
    return value


def assert_no_git_write(before: GitSnapshot, repo: GitRepo, services: ServiceBundle) -> None:
    assert repo.head == before.head
    assert repo.commit_count == before.commits
    assert repo.remote_ref(TARGET_BRANCH) == before.remote_target
    assert not services.github.calls("create_pull_request")


def assert_linear_feature_commit(before: GitSnapshot, repo: GitRepo) -> None:
    assert repo.commit_count == before.commits + 1
    parent = repo.git("rev-parse", "HEAD^").stdout.strip()
    assert parent == before.head
    parents = repo.git("rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 2
    assert repo.remote_ref(TARGET_BRANCH) == repo.head


def delivery_request(spec_path: Path, repo: GitRepo, artifacts, *, mention: str | None = None) -> DeliveryRequest:
    return DeliveryRequest(
        spec_path=spec_path,
        repo_root=repo.root,
        artifacts=artifacts,
        environment={"GITHUB_TOKEN": "write-test-token", "GITHUB_REPOSITORY": "example/phase6"},
        notification_mention=mention,
    )


def github_fixture(name: str) -> dict[str, Any]:
    path = Path(__file__).parent.parent / "fixtures" / "github" / name
    return json.loads(path.read_text(encoding="utf-8"))


def issue_has_label(services: ServiceBundle, label: str) -> bool:
    for call in services.github.calls("create_issue"):
        labels = call.get("labels") or []
        if label in labels or label in json.dumps(call):
            return True
    return False


def assert_reason(result: DeliveryResult, expected: str) -> None:
    assert result.status.upper() == "ESCALATED"
    assert result.reason and expected in result.reason.upper()
