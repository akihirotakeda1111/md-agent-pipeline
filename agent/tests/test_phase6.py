from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from agent.classify import FailureClass
from agent.delivery import assert_commit_allowed, assert_pr_allowed
from agent.errors import AgentError
from agent.notify import EscalationNotice, mention_from_config
from agent.policy import classify_control_plane_error
from agent.pr import build_pr_body, build_pr_title
from agent.reconcile import reconcile_work_unit
from agent.spec import parse_spec
from agent.state import (
    ExecutionStatus,
    apply_transition,
    new_execution_state,
    state_file_path,
    write_state,
)
from agent.summary import render_summary, write_github_summary
from agent.workunit import WorkUnitReport

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SPEC = REPO_ROOT / "specs" / "tasks" / "example-task.md"


def _report(**overrides: object) -> WorkUnitReport:
    spec = parse_spec(EXAMPLE_SPEC)
    state = new_execution_state(spec)
    payload = {
        "outcome": "FINAL_VERIFICATION_PASSED",
        "spec_id": spec.id,
        "spec_path": str(EXAMPLE_SPEC),
        "base_sha": "abc",
        "branch": spec.target_branch,
        "state": state,
        "completed_tasks": ("task-1",),
        "changed_files": ("worker/app.py",),
        "validation_results": ("python check.py",),
        "repair_attempts": 0,
        "final_verification_passed": True,
        "validation_passed": True,
        "scope_allowed": True,
        "message": "ok",
        "skip_reason": None,
        "patch_file": "changes.patch",
        "patch_sha256": "",
    }
    payload.update(overrides)
    return WorkUnitReport(**payload)  # type: ignore[arg-type]


def test_commit_only_after_validation() -> None:
    report = _report(validation_passed=False, final_verification_passed=False, outcome="FAILED")
    with pytest.raises(AgentError) as exc_info:
        assert_commit_allowed(report)
    assert exc_info.value.code == "COMMIT_BEFORE_VALIDATION"


def test_no_commit_on_scope_violation() -> None:
    report = _report(scope_allowed=False, outcome="SCOPE_VIOLATION")
    with pytest.raises(AgentError) as exc_info:
        assert_commit_allowed(report)
    assert exc_info.value.code == "COMMIT_SCOPE_VIOLATION"


def test_no_pr_before_final_verification() -> None:
    report = _report(
        final_verification_passed=False, validation_passed=True, outcome="TASK_COMPLETED"
    )
    with pytest.raises(AgentError) as exc_info:
        assert_pr_allowed(report)
    assert exc_info.value.code == "PR_BEFORE_FINAL_VERIFICATION"


def test_pr_body_generation() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    body = build_pr_body(
        spec,
        completed_tasks=["task-1"],
        changed_files=["worker/app.py"],
        validation_results=["python check.py: PASSED"],
        final_verification="PASSED",
        repair_attempts=1,
        escalation_history=["none"],
    )
    assert build_pr_title(spec).startswith(spec.id)
    for heading in (
        "Task Spec",
        "Objective",
        "Completed Tasks",
        "Changed Files",
        "Validation Results",
        "Final Verification",
        "Repair Attempts",
        "Known Limitations",
        "Human Review Points",
        "Escalation History",
    ):
        assert f"## {heading}" in body
    assert "`task-1`" in body
    assert "`worker/app.py`" in body
    from agent.pr import is_same_work_unit_pull, parse_work_unit_marker

    marker = parse_work_unit_marker(body)
    assert marker == {
        "spec_id": spec.id,
        "base_branch": spec.base_branch,
        "target_branch": spec.target_branch,
    }
    assert is_same_work_unit_pull(
        spec,
        {
            "body": body,
            "head": {"ref": spec.target_branch},
            "base": {"ref": spec.base_branch},
        },
    )


def test_failed_vs_escalated_policy() -> None:
    env = AgentError.environment_failure("api 502", code="GITHUB_API_FAILURE")
    mismatch = AgentError.escalation_required("commit missing", code="STATE_COMMIT_MISMATCH")
    assert classify_control_plane_error(env) is FailureClass.ENVIRONMENT_FAILURE
    assert classify_control_plane_error(mismatch) is FailureClass.ESCALATION_REQUIRED
    assert classify_control_plane_error(
        AgentError.policy_violation("scope", code="SCOPE_VIOLATION")
    ) is (FailureClass.ESCALATION_REQUIRED)


def test_escalation_notification_payload() -> None:
    notice = EscalationNotice(
        task_id="phase2-step2",
        current_task="task-1",
        reason="state/commit mismatch",
        last_validation="python check.py",
        repair_attempts=2,
        required_human_action="Inspect Git history.",
        mention=None,
    )
    markdown = notice.to_markdown()
    assert "`phase2-step2`" in markdown
    assert "`task-1`" in markdown
    assert "state/commit mismatch" in markdown
    assert "python check.py" in markdown
    assert "Inspect Git history." in markdown
    assert "@" not in markdown.split("Mention:", 1)[1]
    payload = notice.to_json_dict()
    assert payload["mention"] is None
    assert mention_from_config() is None


def test_summary_rendering(tmp_path: Path) -> None:
    markdown = render_summary(
        spec_path="specs/tasks/example-task.md",
        task_id="phase2-step2",
        state="PR_CREATED",
        current_task="task-1",
        completed_tasks=["task-1"],
        changed_files=["worker/app.py"],
        validation_results=["python check.py: PASSED"],
        repair_attempts=1,
        pr_url="https://github.com/example/repo/pull/3",
        failure_reason=None,
        escalation_reason=None,
    )
    assert "Task Spec" in markdown
    assert "phase2-step2" in markdown
    assert "PR_CREATED" in markdown
    assert "worker/app.py" in markdown
    path = tmp_path / "summary.md"
    write_github_summary(path, markdown)
    assert "https://github.com/example/repo/pull/3" in path.read_text(encoding="utf-8")


def test_state_commit_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "p6@example.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase6"], cwd=repo, check=True, capture_output=True
    )
    spec = parse_spec(EXAMPLE_SPEC)
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    state = apply_transition(
        apply_transition(new_execution_state(spec), ExecutionStatus.RUNNING, current_task="task-1"),
        ExecutionStatus.IMPLEMENTING,
    )
    state = apply_transition(
        apply_transition(state, ExecutionStatus.VALIDATING),
        ExecutionStatus.TASK_COMPLETED,
        completed_tasks=["task-1"],
        last_result="PASSED",
    )
    write_state(state_file_path(repo, spec.id), state)
    with pytest.raises(AgentError) as exc_info:
        reconcile_work_unit(spec, repo, github=None)
    assert exc_info.value.code == "STATE_COMMIT_MISMATCH"


def test_state_branch_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "p6@example.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase6"], cwd=repo, check=True, capture_output=True
    )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    spec = parse_spec(EXAMPLE_SPEC)
    state = new_execution_state(spec)
    write_state(state_file_path(repo, spec.id), replace(state, branch="feature/wrong"))
    with pytest.raises(AgentError) as exc_info:
        reconcile_work_unit(spec, repo, github=None)
    assert exc_info.value.code == "STATE_BRANCH_MISMATCH"


def test_resume_from_valid_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "p6@example.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase6"], cwd=repo, check=True, capture_output=True
    )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    spec = parse_spec(EXAMPLE_SPEC)
    write_state(state_file_path(repo, spec.id), new_execution_state(spec))
    result = reconcile_work_unit(spec, repo, github=None)
    assert result.action == "continue"
    assert result.should_run_codex is True


def test_resume_from_failed_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "p6@example.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Phase6"], cwd=repo, check=True, capture_output=True
    )
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    spec = parse_spec(EXAMPLE_SPEC)
    running = apply_transition(new_execution_state(spec), ExecutionStatus.RUNNING)
    failed = apply_transition(running, ExecutionStatus.FAILED)
    write_state(state_file_path(repo, spec.id), failed)
    result = reconcile_work_unit(spec, repo, github=None)
    assert result.action == "retry"
    assert result.state.state is ExecutionStatus.RUNNING


def test_github_client_lists_pulls_by_head() -> None:
    from agent.github_api import GitHubClient

    calls: list[tuple[str, str]] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        calls.append((method, url))
        assert headers["Authorization"] == "Bearer tok"
        assert headers["X-GitHub-Api-Version"] == "2026-03-10"
        assert "pulls" in url
        assert "head=" in url
        return 200, [{"number": 9, "html_url": "https://example.test/9"}]

    client = GitHubClient(token="tok", repository="octo/repo", requester=requester)
    pulls = client.list_open_pulls(head_branch="feature/phase2-worker")
    assert pulls[0]["number"] == 9
    assert calls[0][0] == "GET"
    assert "CODEX" not in json.dumps(headers_safe(calls))


def headers_safe(calls: list[tuple[str, str]]) -> dict[str, str]:
    return {"url": calls[0][1]}


def _work_unit_pull(spec: object, *, number: int = 7, **overrides: object) -> dict[str, object]:
    from agent.pr import build_pr_body
    from agent.spec import TaskSpec

    parsed = spec if isinstance(spec, TaskSpec) else parse_spec(EXAMPLE_SPEC)
    pull: dict[str, object] = {
        "number": number,
        "html_url": f"https://example.test/pull/{number}",
        "head": {"ref": parsed.target_branch},
        "base": {"ref": parsed.base_branch},
        "body": build_pr_body(
            parsed,
            completed_tasks=["task-1"],
            changed_files=["src/app.py"],
            validation_results=[],
            final_verification="PASSED",
            repair_attempts=0,
        ),
    }
    pull.update(overrides)
    return pull


class _FakeGitHub:
    def __init__(self, pulls: list[dict[str, object]] | None = None) -> None:
        self.pulls = list(pulls or [])
        self.labels: dict[str, dict[str, str]] = {}
        self.comments: list[tuple[int, str]] = []
        self.issues: list[dict[str, object]] = []
        self.created_pulls = 0

    def list_open_pulls(self, *, head_branch: str) -> list[dict[str, object]]:
        return [pull for pull in self.pulls if True]

    def get_label(self, name: str) -> dict[str, str] | None:
        return self.labels.get(name)

    def create_label(self, *, name: str, color: str, description: str) -> dict[str, str]:
        self.labels[name] = {"name": name, "color": color, "description": description}
        return self.labels[name]

    def add_issue_labels(self, issue_number: int, labels: list[str]) -> None:
        self.labels[f"issue:{issue_number}"] = {"labels": ",".join(labels)}

    def create_pull(self, *, title: str, head: str, base: str, body: str) -> dict[str, object]:
        self.created_pulls += 1
        pull = {
            "number": 12,
            "html_url": "https://example.test/pull/12",
            "title": title,
            "head": {"ref": head},
            "base": {"ref": base},
            "body": body,
        }
        self.pulls.append(pull)
        return pull

    def create_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, object]:
        issue = {"number": 4, "title": title, "body": body, "labels": labels or []}
        self.issues.append(issue)
        return issue

    def create_issue_comment(self, issue_number: int, body: str) -> dict[str, object]:
        self.comments.append((issue_number, body))
        return {"id": 1}


def test_delivery_reuses_existing_pull_request(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = parse_spec(EXAMPLE_SPEC)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(report_dir, _report(patch_sha256=file_sha256(patch)))
    github = _FakeGitHub(pulls=[_work_unit_pull(spec)])
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "PR_CREATED"
    assert result.pr_number == 7
    assert github.created_pulls == 0
    assert "https://example.test/pull/7" in (tmp_path / "summary.md").read_text(encoding="utf-8")


def test_same_branch_pr_without_work_unit_marker_is_not_reused(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = parse_spec(EXAMPLE_SPEC)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(report_dir, _report(patch_sha256=file_sha256(patch)))
    github = _FakeGitHub(
        pulls=[
            {
                "number": 7,
                "html_url": "https://example.test/pull/7",
                "head": {"ref": spec.target_branch},
                "base": {"ref": spec.base_branch},
                "body": "manual PR",
            }
        ]
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "WORK_UNIT_PR_MISMATCH"
    assert github.created_pulls == 0
    assert github.labels.get("issue:7", {}).get("labels") != "agent:ready"


def test_same_branch_pr_with_other_spec_id_is_not_reused(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.pr import build_work_unit_marker
    from agent.workunit import file_sha256, write_work_unit_report

    spec = parse_spec(EXAMPLE_SPEC)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(report_dir, _report(patch_sha256=file_sha256(patch)))
    foreign = build_work_unit_marker(spec).replace(spec.id, "other-spec")
    github = _FakeGitHub(
        pulls=[
            {
                "number": 7,
                "html_url": "https://example.test/pull/7",
                "head": {"ref": spec.target_branch},
                "base": {"ref": spec.base_branch},
                "body": foreign,
            }
        ]
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert github.created_pulls == 0
    assert github.labels.get("issue:7", {}).get("labels") != "agent:ready"


def _bind_report(report_dir: Path, report: WorkUnitReport, patch_text: str = "") -> None:
    from agent.workunit import file_sha256, write_work_unit_report

    report_dir.mkdir(parents=True, exist_ok=True)
    patch = report_dir / report.patch_file
    patch.write_text(patch_text, encoding="utf-8")
    report.patch_sha256 = file_sha256(patch)
    write_work_unit_report(report_dir, report)


def test_context_mismatch_blocks_existing_pr_reuse(tmp_path: Path) -> None:
    from agent.delivery import run_delivery

    spec = parse_spec(EXAMPLE_SPEC)
    report_dir = tmp_path / "report"
    _bind_report(report_dir, _report(spec_id="other-spec"))
    github = _FakeGitHub(pulls=[{"number": 7, "html_url": "https://example.test/pull/7"}])
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert github.created_pulls == 0
    assert github.labels.get("issue:7", {}).get("labels") != "agent:ready"


def test_patch_digest_mismatch_blocks_existing_pr_reuse(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.workunit import write_work_unit_report

    spec = parse_spec(EXAMPLE_SPEC)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    (report_dir / "changes.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    write_work_unit_report(report_dir, _report(patch_sha256="0" * 64))
    github = _FakeGitHub(pulls=[{"number": 7, "html_url": "https://example.test/pull/7"}])
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert github.created_pulls == 0
    assert github.labels.get("issue:7", {}).get("labels") != "agent:ready"


DELIVER_SPEC = """---
schema_version: 1
id: deliver-demo
title: Deliver Demo
status: PENDING
base_branch: main
target_branch: feature/deliver
allowed_paths:
  - src/**
forbidden_paths:
  - specs/**
  - .agent/**
repair_attempt_limit: 1
review_attempt_limit: 1
---

# Objective

Write src/app.py.

# Non-Goals

None.

# Forbidden Actions

Do not edit specs.

# Architecture Invariants

Keep changes in src.

# Tasks

## task-1: Write app

### Requirement

Create src/app.py.

### Acceptance Criteria

- File exists.

### Validation

```text
python -c "print(1)"
```

# Final Verification

```text
python -c "print(1)"
```
"""


def _git(repo: Path, *args: str) -> str:
    import subprocess

    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def test_deliver_scope_rejects_agent_state_in_patch(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.gitwrite import export_patch, head_sha
    from agent.spec import parse_spec as parse
    from agent.workunit import file_sha256, write_work_unit_report

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    (repo / ".gitignore").write_text(".agent/state/*.json\n", encoding="utf-8")
    spec_path = repo / "spec.md"
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", ".gitignore", "spec.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    base = head_sha(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    state_dir = repo / ".agent" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "leaked.json").write_text("{}\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    patch = report_dir / "changes.patch"
    export_patch(repo, base, patch)
    spec = parse(spec_path)
    report = _report(
        spec_id=spec.id,
        spec_path=str(spec_path),
        base_sha=base,
        branch=spec.target_branch,
        changed_files=("src/app.py", ".agent/state/leaked.json"),
        patch_sha256=file_sha256(patch),
    )
    write_work_unit_report(report_dir, report)
    (repo / "src" / "app.py").unlink()
    (state_dir / "leaked.json").unlink()
    github = _FakeGitHub()
    result = run_delivery(
        spec,
        repo_root=repo,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert github.created_pulls == 0
    log = _git(repo, "log", "--oneline")
    assert "feat(" not in log


def test_deliver_head_mismatch_does_not_rebase(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.gitwrite import export_patch, head_sha
    from agent.spec import parse_spec as parse
    from agent.workunit import file_sha256, write_work_unit_report

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    spec_path = repo / "spec.md"
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", "spec.md")
    _git(repo, "commit", "-m", "init")
    base = head_sha(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    export_patch(repo, base, report_dir / "changes.patch")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "ahead")
    _git(repo, "checkout", "-b", "feature/deliver")
    spec = parse(spec_path)
    report = _report(
        spec_id=spec.id,
        spec_path=str(spec_path),
        base_sha=base,
        branch=spec.target_branch,
        changed_files=("src/app.py",),
        patch_sha256=file_sha256(report_dir / "changes.patch"),
    )
    write_work_unit_report(report_dir, report)
    github = _FakeGitHub()
    result = run_delivery(
        spec,
        repo_root=repo,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert "expected" in result.message
    assert github.created_pulls == 0


def test_deliver_commits_after_scope_manifest_and_fv(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.gitwrite import export_patch, head_sha
    from agent.spec import parse_spec as parse
    from agent.workunit import file_sha256, write_work_unit_report

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    spec_path = repo / "spec.md"
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", "spec.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    base = head_sha(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    patch = report_dir / "changes.patch"
    export_patch(repo, base, patch)
    spec = parse(spec_path)
    state = replace(new_execution_state(spec), state=ExecutionStatus.FINAL_VALIDATING)
    report = _report(
        spec_id=spec.id,
        spec_path=str(spec_path),
        base_sha=base,
        branch=spec.target_branch,
        changed_files=("src/app.py",),
        patch_sha256=file_sha256(patch),
        state=state,
    )
    write_work_unit_report(report_dir, report)
    (repo / "src" / "app.py").unlink()
    github = _FakeGitHub()
    result = run_delivery(
        spec,
        repo_root=repo,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "PR_CREATED"
    assert github.created_pulls == 1
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "ok\n"
    assert "feature/deliver" in _git(origin, "branch")


def _path_env() -> dict[str, str]:
    import os
    import sys

    python_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    if python_dir not in path.split(os.pathsep):
        path = python_dir + os.pathsep + path
    env = {"PATH": path}
    if os.environ.get("PATHEXT"):
        env["PATHEXT"] = os.environ["PATHEXT"]
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def test_patch_manifest_mismatch_does_not_commit(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.gitwrite import export_patch, head_sha
    from agent.spec import parse_spec as parse
    from agent.workunit import file_sha256, write_work_unit_report

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    spec_path = repo / "spec.md"
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", "spec.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    base = head_sha(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    patch = report_dir / "changes.patch"
    export_patch(repo, base, patch)
    spec = parse(spec_path)
    report = _report(
        spec_id=spec.id,
        spec_path=str(spec_path),
        base_sha=base,
        branch=spec.target_branch,
        changed_files=("src/other.py",),
        patch_sha256=file_sha256(patch),
        state=replace(new_execution_state(spec), state=ExecutionStatus.FINAL_VALIDATING),
    )
    write_work_unit_report(report_dir, report)
    (repo / "src" / "app.py").unlink()
    github = _FakeGitHub()
    result = run_delivery(
        spec,
        repo_root=repo,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert "changed_files" in result.message or "manifest" in result.message.lower()
    assert github.created_pulls == 0
    assert "feat(" not in _git(repo, "log", "--oneline")


def test_missing_ephemeral_state_restarts_work_unit(tmp_path: Path) -> None:
    from agent.codex_runner import ProcessResult
    from agent.workunit import run_work_unit

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    spec_path = repo / "spec.md"
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", "spec.md")
    _git(repo, "commit", "-m", "init")
    spec = parse_spec(spec_path)
    assert not state_file_path(repo, spec.id).exists()
    started: list[str] = []

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        started.append("codex")
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("ok\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    report = run_work_unit(
        spec_path,
        repo_root=repo,
        report_dir=tmp_path / "out",
        persist_state=False,
        env=_path_env(),
        executor=executor,
    )
    assert started
    assert report.completed_tasks == ("task-1",)
    assert report.outcome == "FINAL_VERIFICATION_PASSED"
    assert not state_file_path(repo, spec.id).exists()


def test_work_unit_report_keeps_repair_attempts_after_final_verification(tmp_path: Path) -> None:
    from agent.codex_runner import ProcessResult
    from agent.workunit import run_work_unit

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    spec_path = repo / "spec.md"
    spec_path.write_text(
        DELIVER_SPEC.replace('python -c "print(1)"', "python check.py"),
        encoding="utf-8",
    )
    (repo / "check.py").write_text(
        "from pathlib import Path\nraise SystemExit(0 if Path('src/app.py').is_file() else 1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "spec.md", "check.py")
    _git(repo, "commit", "-m", "init")
    calls = {"n": 0}

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        calls["n"] += 1
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if calls["n"] > 1:
            dest.write_text("ok\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    report_dir = tmp_path / "out"
    report = run_work_unit(
        spec_path,
        repo_root=repo,
        report_dir=report_dir,
        persist_state=False,
        env=_path_env(),
        executor=executor,
    )
    assert report.outcome == "FINAL_VERIFICATION_PASSED"
    assert report.repair_attempts == 1
    payload = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert payload["repair_attempts"] == 1
    assert payload["state"]["repairAttempts"] == 1


def test_deliver_final_verification_failure_does_not_git_write(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.gitwrite import export_patch, head_sha
    from agent.spec import parse_spec as parse
    from agent.workunit import file_sha256, write_work_unit_report

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    spec_path = repo / "spec.md"
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    (repo / "fail_fv.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    fail_body = DELIVER_SPEC.rsplit("# Final Verification", 1)[0] + (
        "# Final Verification\n\n```text\npython fail_fv.py\n```\n"
    )
    spec_path.write_text(fail_body, encoding="utf-8")
    _git(repo, "add", "spec.md", "fail_fv.py")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    base = head_sha(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    patch = report_dir / "changes.patch"
    export_patch(repo, base, patch)
    spec = parse(spec_path)
    report = _report(
        spec_id=spec.id,
        spec_path=str(spec_path),
        base_sha=base,
        branch=spec.target_branch,
        changed_files=("src/app.py",),
        patch_sha256=file_sha256(patch),
        state=replace(new_execution_state(spec), state=ExecutionStatus.FINAL_VALIDATING),
        final_verification_passed=True,
        validation_passed=True,
    )
    write_work_unit_report(report_dir, report)
    (repo / "src" / "app.py").unlink()
    github = _FakeGitHub()
    result = run_delivery(
        spec,
        repo_root=repo,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome in {"FAILED", "ESCALATED"}
    assert github.created_pulls == 0
    assert "feat(" not in _git(repo, "log", "--oneline")
    listed = _git(origin, "branch")
    assert "feature/deliver" not in listed
