---
schema_version: 1
id: no-final
title: Missing Final Verification
status: PENDING
base_branch: main
target_branch: feat/no-final
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject a missing final verification.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## incomplete: Fixture task
### Requirement
Do not run.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
