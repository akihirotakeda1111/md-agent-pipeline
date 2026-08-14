---
schema_version: 1
id: minimal-spec
title: Minimal valid spec
status: PENDING
base_branch: main
target_branch: feat/minimal
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective
Create the smallest valid change.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## setup: Fixture task
### Requirement
Create `src/example.txt`.
### Acceptance Criteria
The file exists.
### Validation
Run the file-existence check.
# Final Verification
Run all validations.
