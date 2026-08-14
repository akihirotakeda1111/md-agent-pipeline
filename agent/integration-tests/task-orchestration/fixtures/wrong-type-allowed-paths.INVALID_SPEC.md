---
schema_version: 1
id: wrong-paths-type
title: Wrong allowed paths type
status: PENDING
base_branch: main
target_branch: feat/wrong-paths
allowed_paths: src/**
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject a scalar path collection.
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
