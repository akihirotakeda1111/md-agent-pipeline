---
schema_version: 1
id: negative-limit
title: Negative attempt limit
status: PENDING
base_branch: main
target_branch: feat/negative
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: -1
review_attempt_limit: 0
---
# Objective
Reject negative limits.
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
Must not run.
