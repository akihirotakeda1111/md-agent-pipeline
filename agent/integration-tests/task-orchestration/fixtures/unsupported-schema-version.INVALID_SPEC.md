---
schema_version: 2
id: future-schema
title: Unsupported schema
status: PENDING
base_branch: main
target_branch: feat/future
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject unsupported schema versions.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## never: Fixture task
### Requirement
Do not run.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
# Final Verification
Not applicable.
