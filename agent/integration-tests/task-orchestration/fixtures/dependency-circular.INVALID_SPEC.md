---
schema_version: 1
id: circular-dependency
title: Circular dependency
status: PENDING
base_branch: main
target_branch: feat/cycle
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject dependency cycles.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## first: Fixture task
depends_on: second
### Requirement
Do not run.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
## second: Fixture task
depends_on: first
### Requirement
Do not run.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
# Final Verification
Must not run.
