---
schema_version: 1
id: path-edges
title: Valid path edge cases
status: PENDING
base_branch: main
target_branch: feat/path-edges
allowed_paths:
  - "**/*.ts"
  - ".github/workflows/*.yml"
  - "docs/folder with spaces/**"
  - "資料/**"
forbidden_paths:
  - "**/*.pem"
repair_attempt_limit: 1
review_attempt_limit: 1
---
# Objective
Accept safe repository-relative path patterns.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## paths: Fixture task
### Requirement
Process safe path patterns.
### Acceptance Criteria
All declared patterns remain repository-relative.
### Validation
Run path validation tests.
# Final Verification
Confirm no path escapes the repository.
