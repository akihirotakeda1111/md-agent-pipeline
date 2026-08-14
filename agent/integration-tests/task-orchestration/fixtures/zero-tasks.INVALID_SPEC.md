---
schema_version: 1
id: zero-tasks
title: No tasks
status: PENDING
base_branch: main
target_branch: feat/none
allowed_paths: [src/**]
forbidden_paths: []
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Reject a spec with no tasks.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks

# Final Verification
Nothing should run.
