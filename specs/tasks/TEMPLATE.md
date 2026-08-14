---
schema_version: 1
id: your-task-id
title: Your Task Title
status: PENDING
base_branch: main
target_branch: feature/your-task-id

allowed_paths:
  - path/to/application/**

forbidden_paths:
  - specs/**
  - .agent/**
  - agent/**
  - .github/**

repair_attempt_limit: 3
review_attempt_limit: 3
---

# Objective

Describe the outcome this work unit must achieve.

# Non-Goals

List work that this spec must not include.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not change Task Specs, Execution State, or GitHub Workflows.
- Do not run destructive infrastructure commands.

# Architecture Invariants

State the constraints the implementation must preserve.

# Tasks

## task-1: First unit of work

### Requirement

Describe the first implementable unit.

### Acceptance Criteria

- Criterion that can be checked without an LLM.

### Validation

```text
pytest path/to/test_first_unit.py
```

# Final Verification

Describe the commands that must pass after every task is complete.
