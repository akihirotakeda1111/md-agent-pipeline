---
schema_version: 1
id: phase6-single
title: Phase 6 single task
status: PENDING
base_branch: main
target_branch: agent/phase6-single
allowed_paths:
  - app/**
forbidden_paths:
  - .agent/state/**
repair_attempt_limit: 1
review_attempt_limit: 1
---

# Objective

Update `app/result.txt` for a single-task work unit.

# Non-Goals

- Do not implement additional tasks.

# Forbidden Actions

- Do not rewrite git history.

# Architecture Invariants

- Only `app/` may change.

# Tasks

## task-1: Result file

### Requirement

Update `app/result.txt`.

### Acceptance Criteria

- `app/result.txt` exists.

### Validation

```text
python app/check_exists.py app/result.txt
```

# Final Verification

```text
python app/check_exists.py app/result.txt
```
