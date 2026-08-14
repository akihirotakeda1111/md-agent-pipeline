---
schema_version: 1
id: missing-dependency
title: Missing dependency target
status: PENDING
base_branch: main
target_branch: feat/missing-dep
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject an unknown dependency.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## consumer: Fixture task
depends_on: does-not-exist
### Requirement
Do not run.
### Acceptance Criteria
Not applicable.
### Validation
Not applicable.
# Final Verification
Must not run.
