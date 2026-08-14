---
schema_version: 1
id: dependency-chain
title: Ordered dependency chain
status: PENDING
base_branch: main
target_branch: feat/chain
allowed_paths:
  - src/**
  - tests/**
forbidden_paths: [secrets/**]
repair_attempt_limit: 3
review_attempt_limit: 2
---
# Objective
Exercise deterministic task selection.
# Non-Goals

This fixture excludes unrelated work.

# Forbidden Actions

- Do not edit files outside allowed paths.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks
## prepare: Fixture task
### Requirement
Create the interface.
### Acceptance Criteria
The interface is exported.
### Validation
Run the type checker.
## implement: Fixture task
depends_on: prepare
### Requirement
Implement the interface.
### Acceptance Criteria
The implementation satisfies the interface.
### Validation
Run unit tests.
## verify: Fixture task
depends_on: prepare, implement
### Requirement
Add integration coverage.
### Acceptance Criteria
The complete flow is covered.
### Validation
Run integration tests.
# Final Verification
Run type checking and all tests.
