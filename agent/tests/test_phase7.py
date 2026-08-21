"""Phase 7 CodeRabbit review: filter, classify, policy, bounded repair, idempotency."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from agent.codex_runner import ProcessResult
from agent.config import load_config
from agent.errors import AgentError
from agent.github_api import GitHubClient
from agent.gitwrite import head_sha
from agent.pr import build_work_unit_marker
from agent.review_classify import classifier_error_for_http, parse_classification_payload
from agent.review_collect import collect_review_feedback
from agent.review_filter import prefilter_reason
from agent.review_loop import run_review
from agent.review_policy import AUTO_REPAIR_DEFERRED_REASON, decide_review_policy
from agent.review_prepare import prepare_review
from agent.review_track import (
    REVIEW_STATE_START,
    empty_review_track,
    parse_review_track,
    render_review_track,
    with_processed,
)
from agent.review_types import (
    ClassificationResult,
    ReviewClassification,
    ReviewFeedback,
    ReviewPolicyAction,
)
from agent.spec import TaskSpec, parse_spec

SPEC = """---
schema_version: 1
id: review-demo
title: Review Demo
status: PENDING
base_branch: main
target_branch: feature/review
allowed_paths:
  - src/**
forbidden_paths:
  - specs/**
  - .agent/**
repair_attempt_limit: 1
review_attempt_limit: {limit}
---

# Objective

Keep src/app.py valid.

# Non-Goals

No extra frameworks.

# Forbidden Actions

Do not edit specs.

# Architecture Invariants

Keep changes in src.

# Tasks

## task-1: Write app

### Requirement

Create src/app.py with ok.

### Acceptance Criteria

- File exists.

### Validation

```text
python check_app.py
```

# Final Verification

```text
python check_app.py
```
"""

ACTOR = "coderabbitai[bot]"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _repo(tmp_path: Path, *, limit: int = 3) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p7@example.com")
    _git(repo, "config", "user.name", "Phase7")
    spec_dir = repo / "specs" / "tasks"
    spec_dir.mkdir(parents=True)
    (spec_dir / "review-demo.md").write_text(SPEC.format(limit=limit), encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    (repo / "check_app.py").write_text(
        "from pathlib import Path\nassert Path('src/app.py').read_text(encoding='utf-8').strip()\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "feature/review")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "HEAD")
    return repo


def _spec_files(repo: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in (repo / "specs" / "tasks").rglob("*.md"):
        files[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
    return files


def _spec(repo: Path):
    return parse_spec(repo / "specs" / "tasks" / "review-demo.md")


def _fake(repo: Path, spec) -> FakeGithub:
    return FakeGithub(_pull(repo, spec), files=_spec_files(repo))


def _pull(repo: Path, spec) -> dict:
    return {
        "number": 7,
        "state": "open",
        "body": build_work_unit_marker(spec),
        "head": {
            "sha": head_sha(repo),
            "ref": spec.target_branch,
            "repo": {"full_name": "octo/repo"},
        },
        "base": {"ref": spec.base_branch},
    }


def _check_run_event(
    repo: Path,
    *,
    actor: str = ACTOR,
    slug: str = "coderabbitai",
    pull_number: int = 7,
    status: str = "completed",
    conclusion: str = "success",
) -> dict:
    return {
        "sender": {"login": actor},
        "check_run": {
            "head_sha": head_sha(repo),
            "status": status,
            "conclusion": conclusion,
            "app": {"slug": slug},
            "pull_requests": [{"number": pull_number}],
        },
    }


def _comment(
    *,
    source_id: int = 11,
    author: str = ACTOR,
    body: str = "Fix the poller timeout.",
    path: str | None = "src/app.py",
    commit_sha: str | None = None,
    updated_at: str = "2026-08-19T01:00:00Z",
) -> ReviewFeedback:
    kind = "pull_request_review_comment"
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
    identity = f"{kind}:{source_id}:{updated_at}:{digest}"
    return ReviewFeedback(
        kind=kind,
        identity=identity,
        source_id=source_id,
        updated_at=updated_at,
        author=author,
        body=body,
        path=path,
        commit_sha=commit_sha,
    )


def _result(
    classification: ReviewClassification,
    *,
    confidence: float = 0.93,
    paths: tuple[str, ...] = ("src/app.py",),
) -> ClassificationResult:
    return ClassificationResult(classification, confidence, "reason", paths)


def _dummy_spec() -> TaskSpec:
    return TaskSpec(
        schema_version=1,
        id="review-demo",
        title="t",
        status="PENDING",
        base_branch="main",
        target_branch="feature/review",
        allowed_paths=("src/**",),
        forbidden_paths=("specs/**",),
        repair_attempt_limit=1,
        review_attempt_limit=1,
        objective="o",
        non_goals="n",
        forbidden_actions="f",
        architecture_invariants="a",
        tasks=(),
        final_verification="v",
    )


class FakeGithub:
    def __init__(self, pull: dict, *, files: dict[str, str] | None = None) -> None:
        self.pull = pull
        self.files = dict(files or {})
        self.reviews: list[dict] = []
        self.review_comments: list[dict] = []
        self.issue_comments: list[dict] = []
        self.check_runs: list[dict] = []
        self.commit_statuses: list[dict] = []
        self.commit_pulls: list[dict] | None = None
        self.labels: set[str] = set()
        self.issue_labels: set[str] = set()
        self.next_id = 200

    def add_check_run(
        self,
        *,
        head_sha: str,
        conclusion: str = "success",
        status: str = "completed",
        app_slug: str = "coderabbitai",
        completed_at: str = "2026-08-20T00:00:00Z",
    ) -> None:
        self.check_runs.append(
            {
                "id": self.next_id,
                "head_sha": head_sha,
                "status": status,
                "conclusion": conclusion,
                "completed_at": completed_at,
                "app": {"slug": app_slug},
            }
        )
        self.next_id += 1

    def add_commit_status(
        self,
        *,
        sha: str,
        state: str = "success",
        context: str = "CodeRabbit",
        description: str = "Review completed",
        updated_at: str = "2026-08-20T00:00:00Z",
    ) -> None:
        self.commit_statuses.append(
            {
                "id": self.next_id,
                "sha": sha,
                "state": state,
                "context": context,
                "description": description,
                "updated_at": updated_at,
                "creator": {"login": ACTOR},
            }
        )
        self.next_id += 1

    def add_review_comment(
        self,
        *,
        source_id: int = 11,
        author: str = ACTOR,
        body: str = "Fix the poller timeout.",
        path: str = "src/app.py",
        commit_id: str,
        updated_at: str = "2026-08-19T01:00:00Z",
    ) -> None:
        self.review_comments.append(
            {
                "id": source_id,
                "user": {"login": author},
                "body": body,
                "path": path,
                "commit_id": commit_id,
                "updated_at": updated_at,
                "html_url": f"https://example.test/pull/7#discussion-{source_id}",
            }
        )

    def requester(self, method: str, url: str, headers: dict[str, str], data: bytes | None):
        parsed = urlparse(url)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and "/contents/" in path:
            relative = unquote(path.split("/contents/", 1)[1])
            return self._contents(relative, (query.get("ref") or [""])[0], url)
        if method == "GET" and path.endswith("/check-runs"):
            sha = unquote(path.split("/commits/", 1)[1].split("/", 1)[0])
            return 200, {
                "check_runs": [item for item in self.check_runs if item.get("head_sha") == sha]
            }
        if method == "GET" and "/commits/" in path and path.endswith("/statuses"):
            sha = unquote(path.split("/commits/", 1)[1].split("/", 1)[0])
            return 200, [item for item in self.commit_statuses if item.get("sha") == sha]
        if method == "GET" and "/commits/" in path and path.endswith("/pulls"):
            sha = unquote(path.split("/commits/", 1)[1].split("/", 1)[0])
            if self.commit_pulls is not None:
                return 200, list(self.commit_pulls)
            if str((self.pull.get("head") or {}).get("sha") or "") == sha:
                return 200, [self.pull]
            return 200, []
        if method == "GET" and path.endswith("/pulls/7"):
            return 200, self.pull
        if method == "GET" and path.endswith("/pulls/7/reviews"):
            return 200, list(self.reviews)
        if method == "GET" and path.endswith("/pulls/7/comments"):
            return 200, list(self.review_comments)
        if method == "GET" and path.endswith("/issues/7/comments"):
            return 200, list(self.issue_comments)
        if method == "GET" and path.endswith("/issues/7/labels"):
            return 200, [{"name": name} for name in sorted(self.issue_labels)]
        if method == "GET" and "/labels/" in path:
            name = unquote(path.rsplit("/", 1)[-1])
            if name in self.labels:
                return 200, {"name": name}
            raise HTTPError(url, 404, "Not Found", hdrs={}, fp=BytesIO(b'{"message":"Not Found"}'))
        if method == "POST" and path.endswith("/labels") and "/issues/" not in path:
            payload = json.loads(data or b"{}")
            self.labels.add(str(payload["name"]))
            return 201, payload
        if method == "POST" and path.endswith("/issues/7/labels"):
            payload = json.loads(data or b"{}")
            self.issue_labels.update(str(name) for name in payload.get("labels", []))
            return 200, []
        if method == "POST" and path.endswith("/issues/7/comments"):
            payload = json.loads(data or b"{}")
            comment = {
                "id": self.next_id,
                "body": payload["body"],
                "user": {"login": "github-actions[bot]"},
            }
            self.next_id += 1
            self.issue_comments.append(comment)
            return 201, comment
        if method == "PATCH" and "/issues/comments/" in path:
            comment_id = int(path.rsplit("/", 1)[-1])
            payload = json.loads(data or b"{}")
            for comment in self.issue_comments:
                if comment["id"] == comment_id:
                    comment["body"] = payload["body"]
                    return 200, comment
            raise AssertionError(f"missing comment {comment_id}")
        if method == "DELETE" and "/issues/" in path and "/labels/" in path:
            name = unquote(path.rsplit("/", 1)[-1])
            self.issue_labels.discard(name)
            return 200, []
        if method == "DELETE" and "/labels/" in path:
            return 200, []
        raise AssertionError(f"unexpected GitHub call {method} {url} {parse_qs(parsed.query)}")

    def _contents(self, relative: str, ref: str, url: str):
        if relative in self.files:
            encoded = base64.b64encode(self.files[relative].encode("utf-8")).decode("ascii")
            return 200, {
                "type": "file",
                "path": relative,
                "encoding": "base64",
                "content": encoded,
            }
        prefix = relative + "/"
        children: dict[str, dict[str, str]] = {}
        for file_path in self.files:
            if not file_path.startswith(prefix) and relative:
                continue
            rest = file_path[len(prefix) :] if relative else file_path
            name = rest.split("/", 1)[0]
            child_path = f"{prefix}{name}" if relative else name
            if "/" in rest:
                children[name] = {"type": "dir", "path": child_path, "name": name}
            else:
                children[name] = {"type": "file", "path": file_path, "name": name}
        if children:
            return 200, list(children.values())
        raise HTTPError(url, 404, "Not Found", hdrs={}, fp=BytesIO(b'{"message":"Not Found"}'))

    def client(self) -> GitHubClient:
        return GitHubClient(token="tok", repository="octo/repo", requester=self.requester)


def _config(*, auto_repair_enabled: bool | None = None):
    cfg = load_config()
    if auto_repair_enabled is None:
        return cfg
    return replace(cfg, review=replace(cfg.review, auto_repair_enabled=auto_repair_enabled))


def _run(
    repo: Path,
    fake: FakeGithub,
    *,
    classifier=None,
    executor=None,
    auto_repair_enabled: bool | None = None,
):
    return run_review(
        repo_root=repo,
        pull_number=7,
        head_sha_expected=head_sha(repo),
        spec_path="specs/tasks/review-demo.md",
        github=fake.client(),
        classifier=classifier,
        executor=executor,
        config=_config(auto_repair_enabled=auto_repair_enabled),
        env={
            "CODEX_API_KEY": "codex-secret",
            "REVIEW_CLASSIFIER_API_KEY": "review-secret",
            "AGENT_PR_PAT": "pr-create-must-not-leak",
        },
    )


def _observed_loop_cases() -> list[dict]:
    path = Path(__file__).resolve().parent / "fixtures" / "coderabbit_terminal_cases.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [case for case in payload["cases"] if "outcome" in case["expected"]]


def _apply_observed_terminal(
    fake: FakeGithub, case: dict, *, current: str, old: str = "0" * 40
) -> None:
    mapping = {"current": current, "old": old}
    for item in case.get("check_runs") or []:
        bound = dict(item)
        token = bound.get("head_sha")
        if token in mapping:
            bound["head_sha"] = mapping[token]
        fake.check_runs.append(bound)
    for item in case.get("statuses") or []:
        bound = dict(item)
        token = bound.get("sha")
        if token in mapping:
            bound["sha"] = mapping[token]
        fake.commit_statuses.append(bound)


def _assert_exclusive_issue_label(fake: FakeGithub, expected: str) -> None:
    statuses = {"agent:review", "agent:ready", "agent:escalated", "agent:failed"}
    assert expected in fake.issue_labels
    assert not (fake.issue_labels & statuses) - {expected}


def _escalation_notices(fake: FakeGithub) -> list[dict]:
    return [
        comment
        for comment in fake.issue_comments
        if str(comment.get("body") or "").startswith("## Agent escalation")
    ]


def _tracking_comments(fake: FakeGithub) -> list[dict]:
    return [
        comment
        for comment in fake.issue_comments
        if REVIEW_STATE_START in str(comment.get("body") or "")
    ]


def test_non_coderabbit_actor_is_rejected() -> None:
    item = _comment(author="human")
    reason = prefilter_reason(
        item,
        spec=_dummy_spec(),
        runtime_policy=load_config().runtime_edit_policy,
        actor=load_config().coderabbit.actor,
        head_sha="abc",
        processed=set(),
        repo_root=".",
    )
    assert reason == "non-configured-actor"


def test_prepare_skips_non_configured_actor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    result = prepare_review(
        repo_root=repo,
        event_payload=_check_run_event(repo, actor="human", slug="github-actions"),
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "configured CodeRabbit actor" in result.reason
    assert result.coderabbit_actor == load_config().coderabbit.actor
    assert result.to_output_map()["coderabbit_actor"] == load_config().coderabbit.actor


def test_prepare_skips_comment_event(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "action": "created",
            "sender": {"login": ACTOR},
            "issue": {"number": 7, "pull_request": {}},
            "comment": {"body": "Full review finished."},
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "terminal" in result.reason


def test_prepare_skips_human_full_review_instruction(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "action": "created",
            "sender": {"login": "human"},
            "issue": {"number": 7, "pull_request": {}},
            "comment": {"body": "@coderabbitai full review"},
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "terminal" in result.reason


def test_prepare_skips_pending_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "sender": {"login": ACTOR},
            "sha": head_sha(repo),
            "context": "CodeRabbit",
            "state": "pending",
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "terminal" in result.reason


def test_prepare_skips_in_progress_check_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    result = prepare_review(
        repo_root=repo,
        event_payload=_check_run_event(repo, status="in_progress", conclusion=""),
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "terminal" in result.reason


def test_prepare_skips_fork_pull_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    pull = _pull(repo, spec)
    pull["head"]["repo"]["full_name"] = "other/fork"
    fake = FakeGithub(pull)
    result = prepare_review(
        repo_root=repo,
        event_payload=_check_run_event(repo),
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "fork" in result.reason


def test_prepare_skips_pull_number_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    pull = _pull(repo, spec)
    pull["number"] = 99
    fake = FakeGithub(pull)
    result = prepare_review(
        repo_root=repo,
        event_payload=_check_run_event(repo),
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "identity" in result.reason


def test_prepare_skips_stale_event_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    payload = _check_run_event(repo)
    payload["check_run"]["head_sha"] = "0" * 40
    result = prepare_review(
        repo_root=repo,
        event_payload=payload,
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "current pull head" in result.reason


def test_prepare_skips_stale_status_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.commit_pulls = [fake.pull]
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "sender": {"login": ACTOR},
            "sha": "0" * 40,
            "context": "CodeRabbit",
            "state": "success",
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "current pull head" in result.reason


def test_prepare_resolves_spec_from_api_head_not_checkout_tree(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    files = _spec_files(repo)
    (repo / "specs" / "tasks" / "review-demo.md").unlink()
    fake = FakeGithub(_pull(repo, spec), files=files)
    result = prepare_review(
        repo_root=repo,
        event_payload=_check_run_event(repo),
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is True
    assert result.spec_path == "specs/tasks/review-demo.md"
    assert result.coderabbit_actor == ACTOR
    assert result.spec_id == spec.id
    assert result.head_sha == head_sha(repo)


def test_run_review_refuses_stale_api_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    pull = _pull(repo, spec)
    pull["head"]["sha"] = "0" * 40
    result = _run(repo, FakeGithub(pull))
    assert result.outcome == "ESCALATED"
    assert result.code == "PULL_HEAD_MISMATCH"


def test_run_review_refuses_workspace_that_is_not_expected_head(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    merge_sha = "d" * 40
    pull = _pull(repo, spec)
    pull["head"]["sha"] = merge_sha
    result = run_review(
        repo_root=repo,
        pull_number=7,
        head_sha_expected=merge_sha,
        spec_path="specs/tasks/review-demo.md",
        github=FakeGithub(pull).client(),
        env={"CODEX_API_KEY": "codex-secret", "REVIEW_CLASSIFIER_API_KEY": "review-secret"},
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "BASE_SHA_MISMATCH"


def test_duplicate_and_processed_reviews_are_ignored(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    collected = collect_review_feedback(fake.client(), 7, actor=ACTOR)
    assert len(collected) == 1
    track = with_processed(empty_review_track(spec), (collected[0].identity,), increment=False)
    fake.issue_comments.append(
        {
            "id": 9,
            "body": render_review_track(track),
            "user": {"login": "github-actions[bot]"},
        }
    )
    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
    )
    assert result.outcome == "READY_FOR_HUMAN"
    assert result.review_attempts == 0


def test_outdated_review_is_skipped(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id="0" * 40)
    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
    )
    assert result.outcome == "IN_REVIEW"
    assert "current HEAD" in result.message


def test_completed_terminal_with_no_feedback_is_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
    )
    assert result.outcome == "READY_FOR_HUMAN"
    assert result.review_attempts == 0
    _assert_exclusive_issue_label(fake, "agent:ready")
    _assert_single_durable_tracking_comment(fake, spec=spec, head=head_sha(repo))


def test_skipped_terminal_escalates_without_classifier(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo), conclusion="skipped")
    called = {"n": 0}

    def classifier(item, spec):
        called["n"] += 1
        return _result(ReviewClassification.ACTIONABLE)

    def executor(command, *, cwd, env, timeout, stdin):
        raise AssertionError("codex must not run")

    result = _run(repo, fake, classifier=classifier, executor=executor)
    assert result.outcome == "ESCALATED"
    assert result.code == "CODERABBIT_SKIPPED"
    assert called["n"] == 0
    _assert_exclusive_issue_label(fake, "agent:escalated")
    assert len(_escalation_notices(fake)) == 1


def test_skipped_terminal_rerun_keeps_outcome_and_one_notice(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo), conclusion="skipped")
    first = _run(repo, fake)
    assert first.outcome == "ESCALATED"
    assert first.code == "CODERABBIT_SKIPPED"
    _assert_exclusive_issue_label(fake, "agent:escalated")
    assert len(_escalation_notices(fake)) == 1
    second = _run(repo, fake)
    assert second.outcome == "ESCALATED"
    assert second.code == "CODERABBIT_SKIPPED"
    _assert_exclusive_issue_label(fake, "agent:escalated")
    assert len(_escalation_notices(fake)) == 1


def test_failed_terminal_escalates_without_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo), conclusion="timed_out")
    result = _run(repo, fake)
    assert result.outcome == "ESCALATED"
    assert result.code == "CODERABBIT_REVIEW_FAILED"
    _assert_exclusive_issue_label(fake, "agent:escalated")


def test_no_terminal_and_no_feedback_stays_in_review(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    result = _run(repo, fake)
    assert result.outcome == "IN_REVIEW"
    assert "terminal evidence" in result.message
    _assert_exclusive_issue_label(fake, "agent:review")
    tracking = _tracking_comments(fake)
    if tracking:
        parsed = parse_review_track(tracking[0]["body"])
        assert parsed is not None
        assert parsed.head_sha == ""


def test_commit_status_ambiguous_success_escalates(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_commit_status(sha=head_sha(repo), state="success", description="")
    result = _run(repo, fake)
    assert result.outcome == "ESCALATED"
    assert result.code == "CODERABBIT_AMBIGUOUS"
    _assert_exclusive_issue_label(fake, "agent:escalated")


def test_in_progress_terminal_blocks_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo), conclusion="success")
    fake.add_check_run(
        head_sha=head_sha(repo),
        conclusion="",
        status="in_progress",
        completed_at="2026-08-20T01:00:00Z",
    )
    result = _run(repo, fake)
    assert result.outcome == "IN_REVIEW"
    assert "in progress" in result.message
    _assert_exclusive_issue_label(fake, "agent:review")


def test_in_progress_with_feedback_does_not_classify(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(
        head_sha=head_sha(repo),
        conclusion="",
        status="in_progress",
        completed_at="2026-08-20T01:00:00Z",
    )
    called = {"n": 0}

    def classifier(item, spec):
        called["n"] += 1
        return _result(ReviewClassification.UNCERTAIN)

    result = _run(repo, fake, classifier=classifier)
    assert result.outcome == "IN_REVIEW"
    assert called["n"] == 0
    _assert_exclusive_issue_label(fake, "agent:review")


def test_same_head_escalated_is_sticky_against_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo), conclusion="skipped")
    first = _run(repo, fake)
    assert first.outcome == "ESCALATED"
    _assert_exclusive_issue_label(fake, "agent:escalated")
    fake.check_runs.clear()
    fake.add_check_run(head_sha=head_sha(repo), conclusion="success")
    second = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.NON_ACTIONABLE),
    )
    assert second.outcome == "ESCALATED"
    _assert_exclusive_issue_label(fake, "agent:escalated")


def test_new_head_does_not_inherit_previous_terminal(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo), conclusion="skipped")
    first = _run(repo, fake)
    assert first.outcome == "ESCALATED"
    _assert_exclusive_issue_label(fake, "agent:escalated")
    (repo / "src" / "app.py").write_text("next\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "next")
    fake.pull["head"]["sha"] = head_sha(repo)
    fake.check_runs.clear()
    second = _run(repo, fake)
    assert second.outcome == "IN_REVIEW"
    _assert_exclusive_issue_label(fake, "agent:review")


@pytest.mark.parametrize(
    "case",
    _observed_loop_cases(),
    ids=lambda case: str(case["id"]),
)
def test_observed_terminal_loop_outcome_is_stable_on_rerun(tmp_path: Path, case: dict) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    current = head_sha(repo)
    _apply_observed_terminal(fake, case, current=current)
    expected = str(case["expected"]["outcome"])
    first = _run(repo, fake)
    second = _run(repo, fake)
    assert first.outcome == expected
    assert second.outcome == first.outcome
    if case["expected"]["kind"] == "CODERABBIT_SKIPPED":
        assert first.outcome != "READY_FOR_HUMAN"
        assert first.code == "CODERABBIT_SKIPPED"
        assert len(_escalation_notices(fake)) == 1
    if expected == "READY_FOR_HUMAN":
        _assert_exclusive_issue_label(fake, "agent:ready")
        _assert_single_durable_tracking_comment(fake, spec=spec, head=current)
    elif expected == "IN_REVIEW":
        _assert_exclusive_issue_label(fake, "agent:review")
    elif expected == "ESCALATED":
        _assert_exclusive_issue_label(fake, "agent:escalated")


def test_prepare_resolves_check_run_without_pull_number(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    sha = head_sha(repo)
    fake = _fake(repo, spec)
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "sender": {"login": ACTOR},
            "check_run": {
                "head_sha": sha,
                "status": "completed",
                "conclusion": "success",
                "app": {"slug": "coderabbitai"},
                "pull_requests": [],
            },
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is True
    assert result.pull_number == 7
    assert result.head_sha == sha


def test_prepare_accepts_status_event_identity(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    sha = head_sha(repo)
    fake = _fake(repo, spec)
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "sender": {"login": "github-actions[bot]"},
            "sha": sha,
            "context": "CodeRabbit",
            "state": "success",
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is True
    assert result.pull_number == 7


def test_prepare_ambiguous_commit_pulls_is_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    sha = head_sha(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.commit_pulls = [
        {**fake.pull, "number": 7},
        {**fake.pull, "number": 8},
    ]
    result = prepare_review(
        repo_root=repo,
        event_payload={
            "sender": {"login": ACTOR},
            "sha": sha,
            "context": "CodeRabbit",
            "state": "success",
        },
        repository="octo/repo",
        github=fake.client(),
    )
    assert result.should_review is False
    assert "pull request" in result.reason


def test_forbidden_path_review_escalates_without_codex(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    called = {"n": 0}

    def classifier(item, spec):
        called["n"] += 1
        return _result(ReviewClassification.ACTIONABLE)

    def executor(command, *, cwd, env, timeout, stdin):
        raise AssertionError("codex must not run")

    fake.add_review_comment(path="specs/tasks/review-demo.md", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(repo, fake, classifier=classifier, executor=executor)
    assert result.outcome == "ESCALATED"
    assert called["n"] == 0


def test_allowed_path_review_reaches_classifier(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    item = _comment(path="src/app.py", commit_sha=head_sha(repo))
    assert (
        prefilter_reason(
            item,
            spec=spec,
            runtime_policy=load_config().runtime_edit_policy,
            actor=ACTOR,
            head_sha=head_sha(repo),
            processed=set(),
            repo_root=repo,
        )
        is None
    )


def test_valid_and_invalid_classifier_json() -> None:
    parsed = parse_classification_payload(
        {
            "classification": "ACTIONABLE",
            "confidence": 0.93,
            "reason": "Localized implementation correction.",
            "referencedPaths": ["src/app.py"],
        }
    )
    assert parsed.classification is ReviewClassification.ACTIONABLE
    assert parsed.referenced_paths == ("src/app.py",)
    with pytest.raises(AgentError) as caught:
        parse_classification_payload({"classification": "ACTIONABLE"})
    assert caught.value.code == "INVALID_CLASSIFIER_JSON"


@pytest.mark.parametrize(
    ("label", "confidence", "paths", "action"),
    [
        (ReviewClassification.ACTIONABLE, 0.93, ("src/app.py",), ReviewPolicyAction.ESCALATE),
        (ReviewClassification.ACTIONABLE, 0.2, ("src/app.py",), ReviewPolicyAction.ESCALATE),
        (ReviewClassification.NON_ACTIONABLE, 0.99, (), ReviewPolicyAction.IGNORE),
        (ReviewClassification.OUT_OF_SCOPE, 0.99, ("src/app.py",), ReviewPolicyAction.ESCALATE),
        (
            ReviewClassification.CONFLICTS_WITH_SPEC,
            0.99,
            ("src/app.py",),
            ReviewPolicyAction.ESCALATE,
        ),
        (ReviewClassification.UNCERTAIN, 0.99, ("src/app.py",), ReviewPolicyAction.ESCALATE),
    ],
)
def test_mvp_policy_for_each_classification(
    tmp_path: Path, label, confidence, paths, action
) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    decision = decide_review_policy(
        _result(label, confidence=confidence, paths=paths),
        spec,
        runtime_policy=load_config().runtime_edit_policy,
        confidence_threshold=0.80,
        auto_repair_enabled=False,
    )
    assert decision.action is action
    if label is ReviewClassification.ACTIONABLE:
        assert decision.reason == AUTO_REPAIR_DEFERRED_REASON


@pytest.mark.parametrize(
    ("label", "confidence", "paths", "action"),
    [
        (ReviewClassification.ACTIONABLE, 0.93, ("src/app.py",), ReviewPolicyAction.FIX),
        (ReviewClassification.ACTIONABLE, 0.2, ("src/app.py",), ReviewPolicyAction.ESCALATE),
        (ReviewClassification.NON_ACTIONABLE, 0.99, (), ReviewPolicyAction.IGNORE),
        (ReviewClassification.OUT_OF_SCOPE, 0.99, ("src/app.py",), ReviewPolicyAction.ESCALATE),
        (
            ReviewClassification.CONFLICTS_WITH_SPEC,
            0.99,
            ("src/app.py",),
            ReviewPolicyAction.ESCALATE,
        ),
        (ReviewClassification.UNCERTAIN, 0.99, ("src/app.py",), ReviewPolicyAction.ESCALATE),
    ],
)
def test_policy_for_each_classification_when_auto_repair_enabled(
    tmp_path: Path, label, confidence, paths, action
) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    decision = decide_review_policy(
        _result(label, confidence=confidence, paths=paths),
        spec,
        runtime_policy=load_config().runtime_edit_policy,
        confidence_threshold=0.80,
        auto_repair_enabled=True,
    )
    assert decision.action is action


def test_actionable_empty_paths_escalates(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    decision = decide_review_policy(
        _result(ReviewClassification.ACTIONABLE, paths=()),
        spec,
        runtime_policy=load_config().runtime_edit_policy,
        confidence_threshold=0.80,
        auto_repair_enabled=True,
    )
    assert decision.action is ReviewPolicyAction.ESCALATE


def _class_run(
    tmp_path: Path,
    label: ReviewClassification,
    *,
    confidence: float = 0.93,
    auto_repair_enabled: bool | None = None,
):
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    executed = {"n": 0}

    def executor(command, *, cwd, env, timeout, stdin):
        executed["n"] += 1
        Path(cwd, "src", "app.py").write_text("fixed\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(label, confidence=confidence),
        executor=executor,
        auto_repair_enabled=auto_repair_enabled,
    )
    return result, executed["n"]


def test_auto_repair_disabled_actionable_escalates_without_codex(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))

    def executor(command, *, cwd, env, timeout, stdin):
        raise AssertionError("codex must not run")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
        executor=executor,
    )
    assert result.outcome == "ESCALATED"
    assert result.message == AUTO_REPAIR_DEFERRED_REASON
    assert result.review_attempts == 0
    _assert_exclusive_issue_label(fake, "agent:escalated")
    notices = _escalation_notices(fake)
    assert len(notices) == 1
    body = str(notices[0]["body"])
    assert AUTO_REPAIR_DEFERRED_REASON in body
    assert "Inspect CodeRabbit findings" in body
    assert "Repair Attempts: 0" in body


def test_auto_repair_disabled_non_actionable_ready_for_human(tmp_path: Path) -> None:
    result, count = _class_run(tmp_path, ReviewClassification.NON_ACTIONABLE)
    assert result.outcome == "READY_FOR_HUMAN"
    assert result.review_attempts == 0
    assert count == 0


def test_actionable_high_confidence_runs_repair(tmp_path: Path) -> None:
    result, count = _class_run(tmp_path, ReviewClassification.ACTIONABLE, auto_repair_enabled=True)
    assert result.outcome == "REVIEW_FIX_PUSHED"
    assert result.outcome != "READY_FOR_HUMAN"
    assert result.review_attempts == 1
    assert count == 1
    assert result.commit_sha


def test_actionable_low_confidence_escalates(tmp_path: Path) -> None:
    result, count = _class_run(
        tmp_path,
        ReviewClassification.ACTIONABLE,
        confidence=0.2,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert count == 0


def test_non_actionable_ready_for_human(tmp_path: Path) -> None:
    result, count = _class_run(tmp_path, ReviewClassification.NON_ACTIONABLE)
    assert result.outcome == "READY_FOR_HUMAN"
    assert count == 0


def _assert_single_durable_tracking_comment(fake: FakeGithub, *, spec, head: str, previous_id=None):
    tracking = _tracking_comments(fake)
    assert len(tracking) == 1
    comment_id = tracking[0]["id"]
    if previous_id is not None:
        assert comment_id == previous_id
    parsed = parse_review_track(tracking[0]["body"])
    assert parsed is not None
    assert parsed.matches_work_unit(spec)
    assert parsed.head_sha == head
    return comment_id


def test_no_feedback_ready_keeps_one_tracking_comment_across_reruns(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo))
    first = _run(repo, fake)
    assert first.outcome == "READY_FOR_HUMAN"
    assert first.code != "UNSAFE_REVIEW_TRACK"
    _assert_exclusive_issue_label(fake, "agent:ready")
    track_id = _assert_single_durable_tracking_comment(fake, spec=spec, head=head_sha(repo))
    second = _run(repo, fake)
    assert second.outcome == "READY_FOR_HUMAN"
    assert second.code != "UNSAFE_REVIEW_TRACK"
    _assert_exclusive_issue_label(fake, "agent:ready")
    _assert_single_durable_tracking_comment(
        fake, spec=spec, head=head_sha(repo), previous_id=track_id
    )


def test_non_actionable_ready_keeps_one_tracking_comment_across_reruns(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))

    def classifier(item, spec):
        return _result(ReviewClassification.NON_ACTIONABLE)

    first = _run(repo, fake, classifier=classifier)
    assert first.outcome == "READY_FOR_HUMAN"
    assert first.code != "UNSAFE_REVIEW_TRACK"
    _assert_exclusive_issue_label(fake, "agent:ready")
    track_id = _assert_single_durable_tracking_comment(fake, spec=spec, head=head_sha(repo))
    parsed = parse_review_track(_tracking_comments(fake)[0]["body"])
    assert parsed is not None
    assert parsed.processed
    second = _run(repo, fake, classifier=classifier)
    assert second.outcome == "READY_FOR_HUMAN"
    assert second.code != "UNSAFE_REVIEW_TRACK"
    _assert_exclusive_issue_label(fake, "agent:ready")
    _assert_single_durable_tracking_comment(
        fake, spec=spec, head=head_sha(repo), previous_id=track_id
    )


def test_out_of_scope_escalates(tmp_path: Path) -> None:
    result, count = _class_run(tmp_path, ReviewClassification.OUT_OF_SCOPE)
    assert result.outcome == "ESCALATED"
    assert count == 0


def test_conflicts_with_spec_escalates(tmp_path: Path) -> None:
    result, count = _class_run(tmp_path, ReviewClassification.CONFLICTS_WITH_SPEC)
    assert result.outcome == "ESCALATED"
    assert count == 0


def test_uncertain_escalates(tmp_path: Path) -> None:
    result, count = _class_run(tmp_path, ReviewClassification.UNCERTAIN)
    assert result.outcome == "ESCALATED"
    assert count == 0


def test_invalid_classifier_json_escalates(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))

    def classifier(item, spec):
        raise AgentError.invalid_input("bad json", code="INVALID_CLASSIFIER_JSON")

    result = _run(repo, fake, classifier=classifier)
    assert result.outcome == "ESCALATED"
    assert result.code == "INVALID_CLASSIFIER_JSON"


def test_review_attempt_increments_and_limit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, limit=1)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    fake.issue_comments.append(
        {
            "id": 9,
            "body": render_review_track(
                with_processed(empty_review_track(spec), (), increment=True)
            ),
            "user": {"login": "github-actions[bot]"},
        }
    )
    executed = {"n": 0}

    def executor(command, *, cwd, env, timeout, stdin):
        executed["n"] += 1
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "REVIEW_ATTEMPT_LIMIT"
    assert executed["n"] == 0
    parsed = parse_review_track(fake.issue_comments[0]["body"])
    assert parsed is not None
    assert parsed.review_attempts == 1


def test_review_fix_scope_violation(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))

    def executor(command, *, cwd, env, timeout, stdin):
        Path(cwd, "specs", "tasks", "review-demo.md").write_text("tamper\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "REVIEW_SCOPE_VIOLATION"


def test_review_fix_isolates_credentials(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    seen: dict[str, str | None] = {}

    def executor(command, *, cwd, env, timeout, stdin):
        seen["CODEX_API_KEY"] = env.get("CODEX_API_KEY")
        seen["REVIEW_CLASSIFIER_API_KEY"] = env.get("REVIEW_CLASSIFIER_API_KEY")
        seen["GITHUB_TOKEN"] = env.get("GITHUB_TOKEN")
        seen["AGENT_PR_PAT"] = env.get("AGENT_PR_PAT")
        assert "Fix the poller timeout." in stdin
        Path(cwd, "src", "app.py").write_text("fixed\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "REVIEW_FIX_PUSHED"
    assert seen["CODEX_API_KEY"] == "codex-secret"
    assert seen["REVIEW_CLASSIFIER_API_KEY"] is None
    assert seen["GITHUB_TOKEN"] is None
    assert seen["AGENT_PR_PAT"] is None


def test_tracking_comment_is_github_durable_not_agent_state() -> None:
    body = render_review_track(
        with_processed(empty_review_track(_dummy_spec()), ("a:1:t:deadbeef",), increment=True)
    )
    parsed = parse_review_track(body)
    assert parsed is not None
    assert parsed.review_attempts == 1
    assert "a:1:t:deadbeef" in parsed.processed
    assert parsed.schema_version == 1
    assert parsed.base_branch == "main"
    assert parsed.target_branch == "feature/review"
    assert ".agent/state" not in body


def test_classifier_request_uses_structured_output_and_review_key(tmp_path: Path) -> None:
    from agent.review_classify import classify_review_comment

    repo = _repo(tmp_path)
    spec = _spec(repo)
    captured: dict[str, object] = {}

    def requester(body: dict, key: str) -> dict:
        captured["body"] = body
        captured["key"] = key
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "classification": "NON_ACTIONABLE",
                                "confidence": 0.9,
                                "reason": "nit",
                                "referencedPaths": [],
                            }
                        )
                    }
                }
            ]
        }

    result = classify_review_comment(
        _comment(),
        spec,
        api_key="review-secret",
        requester=requester,
    )
    assert result.classification is ReviewClassification.NON_ACTIONABLE
    assert captured["key"] == "review-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "gpt-5.4-nano-2026-03-17"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_duplicate_outdated_event_does_not_mark_ready(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.add_review_comment(commit_id="0" * 40)
    collected = collect_review_feedback(fake.client(), 7, actor=ACTOR)
    track = with_processed(
        empty_review_track(spec),
        (collected[0].identity,),
        increment=False,
        head_sha="0" * 40,
    )
    fake.issue_comments.append(
        {
            "id": 9,
            "body": render_review_track(track),
            "user": {"login": "github-actions[bot]"},
        }
    )
    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
    )
    assert result.outcome == "IN_REVIEW"


def test_edited_review_is_reprocessed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    sha = head_sha(repo)
    fake.add_review_comment(body="old text", commit_id=sha, updated_at="2026-08-19T01:00:00Z")
    old = collect_review_feedback(fake.client(), 7, actor=ACTOR)[0]
    fake.review_comments.clear()
    fake.add_review_comment(
        body="edited actionable text",
        commit_id=sha,
        updated_at="2026-08-19T02:00:00Z",
    )
    fake.add_check_run(head_sha=sha)
    track = with_processed(empty_review_track(spec), (old.identity,), increment=False)
    fake.issue_comments.append(
        {
            "id": 9,
            "body": render_review_track(track),
            "user": {"login": "github-actions[bot]"},
        }
    )
    classified = {"n": 0}

    def classifier(item, spec):
        classified["n"] += 1
        return _result(ReviewClassification.NON_ACTIONABLE)

    result = _run(repo, fake, classifier=classifier)
    assert classified["n"] == 1
    assert result.outcome == "READY_FOR_HUMAN"


def test_spoofed_tracking_comment_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    fake.issue_comments.append(
        {
            "id": 9,
            "body": render_review_track(empty_review_track(spec)),
            "user": {"login": "human"},
        }
    )
    result = _run(repo, fake)
    assert result.outcome == "ESCALATED"
    assert result.code == "UNSAFE_REVIEW_TRACK"


def test_classifier_refusal_and_incomplete_are_fail_closed() -> None:
    from agent.review_classify import _extract_message_json

    with pytest.raises(AgentError) as refused:
        _extract_message_json(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": None, "refusal": "no"},
                    }
                ]
            }
        )
    assert refused.value.code == "CLASSIFIER_REFUSAL"
    with pytest.raises(AgentError) as truncated:
        _extract_message_json(
            {
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": '{"classification":'},
                    }
                ]
            }
        )
    assert truncated.value.code == "CLASSIFIER_INCOMPLETE"


def test_classifier_http_429_and_5xx_are_fail_closed() -> None:
    assert classifier_error_for_http(429).code == "CLASSIFIER_API_RATE_LIMIT"
    assert classifier_error_for_http(503).code == "CLASSIFIER_API_FAILURE"
    assert classifier_error_for_http(408).code == "CLASSIFIER_API_FAILURE"


def test_protected_path_review_escalates_without_codex(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    (repo / "agent").mkdir()
    (repo / "agent" / "config.json").write_text("{}\n", encoding="utf-8")
    called = {"n": 0}

    def classifier(item, spec):
        called["n"] += 1
        return _result(ReviewClassification.ACTIONABLE)

    def executor(command, *, cwd, env, timeout, stdin):
        raise AssertionError("codex must not run")

    fake.add_review_comment(path="agent/config.json", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(repo, fake, classifier=classifier, executor=executor)
    assert result.outcome == "ESCALATED"
    assert called["n"] == 0


def test_protected_referenced_paths_escalate_without_codex(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))
    called = {"n": 0}

    def classifier(item, spec):
        return _result(ReviewClassification.ACTIONABLE, paths=("agent/config.json",))

    def executor(command, *, cwd, env, timeout, stdin):
        called["n"] += 1
        raise AssertionError("codex must not run")

    fake.add_review_comment(path="src/app.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(
        repo,
        fake,
        classifier=classifier,
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert called["n"] == 0


def test_review_repair_rejects_protected_path_change(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))

    def executor(command, *, cwd, env, timeout, stdin):
        dest = Path(cwd) / "specs" / "tasks" / "leaked.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("nope\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "REVIEW_SCOPE_VIOLATION"


def test_review_repair_does_not_reload_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    fake = FakeGithub(_pull(repo, spec))

    def boom(*_args: object, **_kwargs: object):
        raise AssertionError("config must not be reloaded from PR HEAD")

    monkeypatch.setattr("agent.review_loop.load_config", boom)
    monkeypatch.setattr("agent.codex_runner.load_config", boom)
    monkeypatch.setattr("agent.cycle.load_config", boom)

    def executor(command, *, cwd, env, timeout, stdin):
        Path(cwd, "src", "app.py").write_text("fixed\n", encoding="utf-8")
        rewritten = Path(cwd) / "agent" / "config.json"
        rewritten.parent.mkdir(parents=True, exist_ok=True)
        rewritten.write_text("{}", encoding="utf-8")
        return ProcessResult(0, "", "")

    fake.add_review_comment(commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(ReviewClassification.ACTIONABLE),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "REVIEW_SCOPE_VIOLATION"
