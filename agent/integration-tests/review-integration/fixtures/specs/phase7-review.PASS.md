---
schema_version: 1
id: phase7-integration
title: Phase 7 review integration contract
status: PENDING
base_branch: main
target_branch: agent/phase7-integration
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

Exercise the asynchronous Phase 7 review contract for an existing pull request.

# Non-Goals

- Do not merge the pull request.
- Do not wait inside the execute workflow for CodeRabbit.

# Forbidden Actions

- Do not rewrite git history.
- Do not pass GitHub write credentials to Codex.

# Architecture Invariants

- CodeRabbit is a review source only.
- The semantic classifier classifies meaning only.
- Deterministic Production policy decides actions.
- Codex implements only accepted repairs and has no GitHub write authority.

# Tasks

## task-1: Preserve first task acceptance

### Requirement

Keep `app/task-one.txt` valid.

### Acceptance Criteria

- `app/task-one.txt` contains `ready`.

### Validation

```text
python app/check_content.py app/task-one.txt ready
```

## task-2: Preserve second task acceptance

depends_on: task-1

### Requirement

Keep `app/task-two.txt` valid.

### Acceptance Criteria

- `app/task-two.txt` contains `ready`.

### Validation

```text
python app/check_content.py app/task-two.txt ready
```

# Final Verification

```text
python app/check_content.py app/review.txt repaired
python app/check_content.py app/task-one.txt ready
python app/check_content.py app/task-two.txt ready
```
