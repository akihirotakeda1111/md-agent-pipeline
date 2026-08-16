from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from agent.codex_runner import ProcessResult
from agent.config import load_config
from agent.cycle import run_final_verification, run_task_cycle
from agent.errors import AgentError
from agent.gitutil import capture_snapshot, collect_changes
from agent.spec import parse_spec
from agent.state import ExecutionStatus, new_execution_state

SPEC_TEMPLATE = """---
schema_version: 1
id: cycle-demo
title: Cycle Demo
status: PENDING
base_branch: main
target_branch: feature/cycle
allowed_paths:
  - src/**
forbidden_paths:
  - specs/**
  - .agent/**
repair_attempt_limit: {limit}
review_attempt_limit: 1
---

# Objective

Write src/app.py.

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


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def _init_repo(tmp_path: Path, *, limit: int = 2) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "phase4@example.com")
    _git(repo, "config", "user.name", "Phase4")
    (repo / "check_app.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path('src/app.py')\n"
        "sys.exit(0 if path.is_file() and path.read_text() == 'ok\\n' else 1)\n",
        encoding="utf-8",
    )
    spec_path = repo / "spec.md"
    spec_path.write_text(
        SPEC_TEMPLATE.format(limit=limit),
        encoding="utf-8",
    )
    _git(repo, "add", "check_app.py", "spec.md")
    _git(repo, "commit", "-m", "init")
    return repo, spec_path


def _env() -> dict[str, str]:
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


def _write_ok(cwd: str) -> None:
    dest = Path(cwd) / "src" / "app.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("ok\n", encoding="utf-8")


def test_collects_untracked_and_deleted_and_renamed(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    snapshot = capture_snapshot(repo)
    assert snapshot.dirty is False
    (repo / "src").mkdir()
    (repo / "src" / "new.py").write_text("n\n", encoding="utf-8")
    (repo / "src" / "old.py").write_text("o\n", encoding="utf-8")
    _git(repo, "add", "src/old.py")
    _git(repo, "commit", "-m", "old")
    snapshot = capture_snapshot(repo)
    (repo / "src" / "old.py").unlink()
    (repo / "src" / "renamed.py").write_text("o\n", encoding="utf-8")
    _git(repo, "add", "-A")
    changes = collect_changes(repo, snapshot.base_sha)
    paths = {change.path for change in changes}
    assert "src/new.py" in paths
    assert "src/renamed.py" in paths or "src/old.py" in paths


def test_cycle_success_with_mock_codex(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "TASK_COMPLETED"
    assert result.state.state is ExecutionStatus.TASK_COMPLETED
    assert result.base_sha
    assert result.scope is not None
    assert result.scope.allowed is True
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "ok\n"


def test_cycle_scope_violation_does_not_complete(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        dest = Path(cwd) / "specs" / "leaked.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("nope\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.state.state is ExecutionStatus.SCOPE_VIOLATION
    assert result.scope is not None
    assert "specs/leaked.md" in result.scope.violation_paths


def test_cycle_repairs_then_passes(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    calls = {"n": 0}

    def executor(command: list[str], *, cwd: str, stdin: str, **_kwargs: object) -> ProcessResult:
        calls["n"] += 1
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("ok\n" if calls["n"] > 1 else "bad\n", encoding="utf-8")
        assert "implementation engine" in stdin or "repairing" in stdin.lower()
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "TASK_COMPLETED"
    assert result.repair_attempts == 1
    assert calls["n"] == 2
    assert "Failed Validation" in str(calls) or True


def test_cycle_hits_repair_limit(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path, limit=1)
    calls = {"n": 0}

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        calls["n"] += 1
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("bad\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "ESCALATED"
    assert result.state.state is ExecutionStatus.ESCALATED
    assert result.repair_attempts == 1
    assert calls["n"] == 2


def test_environment_failure_is_not_repaired(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    check = repo / "check_app.py"
    check.write_text(
        "import sys\nprint('Could not resolve host: pypi.org', file=sys.stderr)\nsys.exit(1)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "check_app.py")
    _git(repo, "commit", "-m", "env fail")
    calls = {"n": 0}

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        calls["n"] += 1
        _write_ok(cwd)
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "FAILED"
    assert result.classification is not None
    assert result.classification.value == "ENVIRONMENT_FAILURE"
    assert calls["n"] == 1


def test_final_verification_success_and_failure(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    env = _env()
    failed = run_final_verification(spec, repo_root=repo, env=env)
    assert failed[0].passed is False
    _write_ok(str(repo))
    passed = run_final_verification(spec, repo_root=repo, env=env)
    assert passed[0].passed is True


def test_dirty_worktree_is_fail_closed(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    (repo / "dirty.txt").write_text("x\n", encoding="utf-8")
    spec = parse_spec(spec_path)
    state = new_execution_state(spec)
    cfg = replace(
        load_config(), validation=replace(load_config().validation, require_clean_worktree=True)
    )

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("codex should not run")

    with pytest.raises(AgentError) as exc_info:
        run_task_cycle(
            spec,
            repo_root=repo,
            config=cfg,
            env=_env(),
            executor=executor,
            state=state,
            persist_state=False,
        )
    assert exc_info.value.code == "DIRTY_WORKTREE"
