---
schema_version: 1
id: duplicate-task
title: Duplicate task IDs
status: PENDING
base_branch: main
target_branch: feat/duplicate
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject duplicate stable IDs.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## same: Fixture task
### Requirement
First task.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
## same: Fixture task
### Requirement
Second task.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
# Final Verification
Must not run.
