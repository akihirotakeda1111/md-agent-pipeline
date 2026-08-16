from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from agent.cli import EXIT_INVALID, EXIT_OK, run_prepare_intake
from agent.errors import AgentError
from agent.intake import (
    assert_execution_guard,
    assert_required_history,
    evaluate_intake,
    write_github_output,
)
from agent.spec import parse_spec
from agent.state import (
    ExecutionStatus,
    apply_transition,
    init_state,
    state_file_path,
    write_state,
)

SPEC_TEMPLATE = """---
schema_version: 1
id: intake-demo
title: Intake Demo
status: PENDING
base_branch: main
target_branch: feature/intake
allowed_paths:
  - src/**
forbidden_paths:
  - specs/**
repair_attempt_limit: 1
review_attempt_limit: 1
---

# Objective

Demo.

# Non-Goals

None.

# Forbidden Actions

None.

# Architecture Invariants

Keep src.

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
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "phase5@example.com")
    _git(repo, "config", "user.name", "Phase5")
    spec_dir = repo / "specs" / "tasks"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "demo.md"
    spec_path.write_text(SPEC_TEMPLATE, encoding="utf-8")
    _git(repo, "add", "specs/tasks/demo.md")
    _git(repo, "commit", "-m", "add spec")
    return repo, spec_path


def test_write_github_output_uses_name_value_lines(tmp_path: Path) -> None:
    path = tmp_path / "output"
    write_github_output(path, {"task_id": "intake-demo", "valid": "true", "should_execute": "true"})
    assert (
        path.read_text(encoding="utf-8") == "task_id=intake-demo\nvalid=true\nshould_execute=true\n"
    )


def test_write_github_output_flattens_multiline_values(tmp_path: Path) -> None:
    path = tmp_path / "output"
    write_github_output(path, {"reason": "line one\nline two\r\nline three"})
    assert path.read_text(encoding="utf-8") == "reason=line one line two line three\n"


def test_push_intake_exposes_task_id(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    result = evaluate_intake(
        repo_root=repo,
        event_name="push",
        ref_name="main",
        sha=sha,
        before_sha="0" * 40,
    )
    assert result.valid is True
    assert result.should_execute is True
    assert result.task_id == "intake-demo"
    assert result.spec_path == "specs/tasks/demo.md"
    assert result.base_branch == "main"
    assert result.target_branch == "feature/intake"


def test_invalid_spec_is_not_executable(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec_path.write_text("---\nid: [\n---\n# Objective\n\nx\n", encoding="utf-8")
    _git(repo, "add", "specs/tasks/demo.md")
    _git(repo, "commit", "-m", "break spec")
    before = _git(repo, "rev-parse", "HEAD^")
    sha = _git(repo, "rev-parse", "HEAD")
    result = evaluate_intake(
        repo_root=repo,
        event_name="push",
        ref_name="main",
        sha=sha,
        before_sha=before,
    )
    assert result.valid is False
    assert result.should_execute is False
    assert result.task_id == ""
    output_path = tmp_path / "github_output"
    exit_code = run_prepare_intake(
        [
            "--repo-root",
            str(repo),
            "--event-name",
            "push",
            "--ref-name",
            "main",
            "--sha",
            sha,
            "--before",
            before,
            "--github-output",
            str(output_path),
        ]
    )
    assert exit_code == EXIT_INVALID
    assert "valid=false" in output_path.read_text(encoding="utf-8")
    assert "should_execute=false" in output_path.read_text(encoding="utf-8")


def test_feature_branch_push_is_not_task_intake(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    _git(repo, "checkout", "-b", "feature/intake")
    spec_path.write_text(SPEC_TEMPLATE.replace("Demo.", "Changed."), encoding="utf-8")
    _git(repo, "add", "specs/tasks/demo.md")
    _git(repo, "commit", "-m", "agent spec edit")
    before = _git(repo, "rev-parse", "HEAD^")
    sha = _git(repo, "rev-parse", "HEAD")
    result = evaluate_intake(
        repo_root=repo,
        event_name="push",
        ref_name="feature/intake",
        sha=sha,
        before_sha=before,
    )
    assert result.valid is True
    assert result.should_execute is False
    assert "feature-branch" in result.reason
    output_path = tmp_path / "github_output"
    exit_code = run_prepare_intake(
        [
            "--repo-root",
            str(repo),
            "--event-name",
            "push",
            "--ref-name",
            "feature/intake",
            "--sha",
            sha,
            "--before",
            before,
            "--github-output",
            str(output_path),
        ]
    )
    assert exit_code == EXIT_OK
    written = output_path.read_text(encoding="utf-8")
    assert "valid=true" in written
    assert "should_execute=false" in written


def test_required_history_is_available(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    assert_required_history(repo, base_branch="main")


def test_execution_guard_blocks_in_flight_and_terminal(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    pending = init_state(spec, repo)
    assert_execution_guard(spec, repo)
    running = apply_transition(pending, ExecutionStatus.RUNNING, current_task="task-1")
    write_state(state_file_path(repo, spec.id), running)
    with pytest.raises(AgentError) as in_flight:
        assert_execution_guard(spec, repo)
    assert in_flight.value.code == "EXECUTION_GUARD"
    failed = apply_transition(running, ExecutionStatus.FAILED)
    write_state(state_file_path(repo, spec.id), failed)
    with pytest.raises(AgentError) as terminal:
        assert_execution_guard(spec, repo)
    assert "FAILED" in str(terminal.value)


def test_dispatch_rejects_path_escape(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    result = evaluate_intake(
        repo_root=repo,
        event_name="workflow_dispatch",
        ref_name="main",
        sha=sha,
        spec_path="../README.md",
    )
    assert result.valid is False
    assert result.should_execute is False


def test_multiple_spec_changes_are_not_intake(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec_path.write_text(SPEC_TEMPLATE.replace("Demo.", "Changed."), encoding="utf-8")
    second = spec_path.parent / "other.md"
    second.write_text(SPEC_TEMPLATE.replace("intake-demo", "intake-two"), encoding="utf-8")
    _git(repo, "add", "specs/tasks/demo.md", "specs/tasks/other.md")
    _git(repo, "commit", "-m", "two specs")
    before = _git(repo, "rev-parse", "HEAD^")
    sha = _git(repo, "rev-parse", "HEAD")
    result = evaluate_intake(
        repo_root=repo,
        event_name="push",
        ref_name="main",
        sha=sha,
        before_sha=before,
    )
    assert result.valid is False
    assert result.should_execute is False
    assert "multiple" in result.reason
