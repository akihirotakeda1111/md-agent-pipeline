---
schema_version: 1
id: __TASK_ID__
title: Phase 5 feature-branch skip
status: PENDING
base_branch: __BASE_BRANCH__
target_branch: __TARGET_BRANCH__
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 1
review_attempt_limit: 0
---
# Objective

Prove that a valid Task Spec pushed to a non-base branch is parsed and then skipped.

# Non-Goals

- Do not execute Codex.
- Do not create pull requests.

# Forbidden Actions

- Do not edit files outside allowed_paths.
- Do not run git write operations.

# Architecture Invariants

Feature-branch pushes are not task intake. `should_execute` must be false when `ref_name` is not `base_branch`.

# Tasks

## probe: No implementation

depends_on: []

### Requirement

This task must not run. Intake should skip execute on a non-base branch.

### Acceptance Criteria

- Production parse-spec succeeds.
- Production execute is skipped.

### Validation

```text
python -c "raise SystemExit('skip case must not reach validation')"
```

# Final Verification

```text
python -c "raise SystemExit('skip case must not reach final verification')"
```
