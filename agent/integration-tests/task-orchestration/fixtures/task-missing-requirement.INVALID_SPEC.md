---
schema_version: 1
id: no-requirement
title: Missing Requirement
status: PENDING
base_branch: main
target_branch: feat/no-requirement
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
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
# Final Verification
Must not run.
