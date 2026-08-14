---
schema_version: 1
id: no-validation
title: Missing Validation
status: PENDING
base_branch: main
target_branch: feat/no-validation
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject an incomplete task.
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
# Final Verification
Must not run.
