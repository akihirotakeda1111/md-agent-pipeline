from __future__ import annotations

from agent.config import load_config
from agent.gitutil import GitChange, normalize_git_path
from agent.scope import check_scope, path_is_in_scope, path_matches
from agent.spec import parse_spec_text

SPEC = """---
schema_version: 1
id: scope-demo
title: Scope Demo
status: PENDING
base_branch: main
target_branch: feature/scope
allowed_paths:
  - src/**
  - src/exact.py
forbidden_paths:
  - src/secret/**
  - specs/**
repair_attempt_limit: 1
review_attempt_limit: 1
---

# Objective

Scope check.

# Non-Goals

None.

# Forbidden Actions

None.

# Architecture Invariants

Keep secrets out of scope.

# Tasks

## task-1: Touch src

### Requirement

Edit src.

### Acceptance Criteria

- src files only.

### Validation

```text
python -c "print(1)"
```

# Final Verification

```text
python -c "print(1)"
```
"""


def _spec():
    return parse_spec_text(SPEC)


def _policy():
    return load_config().runtime_edit_policy


def test_dot_directories_are_not_stripped() -> None:
    assert normalize_git_path(".agent/state/x.json") == ".agent/state/x.json"
    assert normalize_git_path("./src/app.py") == "src/app.py"


def test_exact_file_and_directory_glob() -> None:
    spec = _spec()
    assert path_matches("src/app.py", "src/**")
    assert path_matches("src/nested/x.py", "src/**")
    assert path_matches("src/exact.py", "src/exact.py")
    assert not path_matches("src/exact.py.bak", "src/exact.py")
    assert not path_matches("other/app.py", "src/**")
    assert path_is_in_scope("src/app.py", spec, _policy())
    assert not path_is_in_scope("README.md", spec, _policy())


def test_nested_and_overlapping_allow_deny() -> None:
    spec = _spec()
    assert path_is_in_scope("src/ok.py", spec, _policy())
    assert not path_is_in_scope("src/secret/key.txt", spec, _policy())
    assert not path_is_in_scope("specs/tasks/x.md", spec, _policy())
    assert not path_is_in_scope(".agent/state/x.json", spec, _policy())


def test_allowed_file_passes_scope() -> None:
    spec = _spec()
    result = check_scope(spec, [GitChange(path="src/app.py", status="modified")], _policy())
    assert result.allowed is True
    assert result.changed_paths == ("src/app.py",)


def test_forbidden_file_is_scope_violation() -> None:
    spec = _spec()
    result = check_scope(spec, [GitChange(path="src/secret/key.txt", status="added")], _policy())
    assert result.allowed is False
    assert result.reason == "SCOPE_VIOLATION"
    assert result.violation_paths == ("src/secret/key.txt",)


def test_untracked_deleted_and_renamed_files() -> None:
    spec = _spec()
    untracked = check_scope(spec, [GitChange(path="src/new.py", status="untracked")], _policy())
    assert untracked.allowed is True
    deleted = check_scope(spec, [GitChange(path="src/old.py", status="deleted")], _policy())
    assert deleted.allowed is True
    renamed_ok = check_scope(
        spec,
        [GitChange(path="src/b.py", status="renamed", old_path="src/a.py")],
        _policy(),
    )
    assert renamed_ok.allowed is True
    renamed_bad = check_scope(
        spec,
        [GitChange(path="src/b.py", status="renamed", old_path="specs/a.md")],
        _policy(),
    )
    assert renamed_bad.allowed is False
    assert "specs/a.md" in renamed_bad.violation_paths


def test_protected_wins_over_allowed_and_forbidden_wins_over_allowed() -> None:
    spec = _spec()
    policy = _policy()
    assert path_is_in_scope("src/ok.py", spec, policy)
    assert not path_is_in_scope("agent/config.json", spec, policy)
    assert not path_is_in_scope("src/secret/key.txt", spec, policy)
    assert not path_is_in_scope("README.md", spec, policy)


def test_protected_file_mutations_are_denied() -> None:
    spec = _spec()
    policy = _policy()
    added = check_scope(spec, [GitChange(path="agent/config.json", status="added")], policy)
    assert added.allowed is False
    modified = check_scope(
        spec,
        [GitChange(path=".github/workflows/x.yml", status="modified")],
        policy,
    )
    assert modified.allowed is False
    deleted = check_scope(spec, [GitChange(path="specs/tasks/x.md", status="deleted")], policy)
    assert deleted.allowed is False
    renamed = check_scope(
        spec,
        [GitChange(path="src/ok.py", status="renamed", old_path="agent/config.json")],
        policy,
    )
    assert renamed.allowed is False
    assert "agent/config.json" in renamed.violation_paths
