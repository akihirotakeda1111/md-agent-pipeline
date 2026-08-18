---
schema_version: 1
id: phase6-integration
title: Phase 6 integration contract
status: PENDING
base_branch: main
target_branch: agent/phase6-integration
allowed_paths:
  - app/**
forbidden_paths:
  - app/forbidden/**
  - .agent/state/**
  - specs/**
  - agent/**
  - .github/**
repair_attempt_limit: 2
review_attempt_limit: 1
---

# Objective

Exercise the Phase 6 work-unit and delivery contract with two ordered tasks.

# Non-Goals

- Do not implement GitHub Actions orchestration in this fixture.
- Do not edit files outside `allowed_paths`.

# Forbidden Actions

- Do not force-push or rewrite git history.
- Do not merge pull requests.

# Architecture Invariants

- Task-1 must complete before task-2.
- Runtime state belongs in `.agent/state`, not in this Task Spec.

# Tasks

## task-1: First result

### Requirement

Create `app/task-1.txt` with the first result.

### Acceptance Criteria

- `app/task-1.txt` exists.

### Validation

```text
python app/check_exists.py app/task-1.txt
```

## task-2: Second result

depends_on: task-1

### Requirement

Create `app/task-2.txt` after task-1.

### Acceptance Criteria

- `app/task-2.txt` exists.

### Validation

```text
python app/check_exists.py app/task-2.txt
```

# Final Verification

```text
python app/check_exists.py app/task-1.txt
python app/check_exists.py app/task-2.txt
```
