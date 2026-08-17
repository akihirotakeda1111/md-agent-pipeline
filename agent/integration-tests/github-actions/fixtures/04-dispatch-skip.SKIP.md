---
schema_version: 1
id: __TASK_ID__
title: Phase 5 workflow_dispatch skip
status: PENDING
base_branch: __BASE_BRANCH__
target_branch: __TARGET_BRANCH__
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 1
review_attempt_limit: 0
---
# Objective

Prove that workflow_dispatch of a valid Task Spec on a non-base ref is parsed and then skipped.

# Non-Goals

- Do not execute Codex.
- Do not create pull requests.
- Do not reuse the sample worker Task Spec.

# Forbidden Actions

- Do not edit files outside allowed_paths.
- Do not run git write operations.

# Architecture Invariants

Manual trigger on a ref other than `base_branch` is not task intake. `should_execute` must be false.

# Tasks

## probe: No implementation

depends_on: []

### Requirement

This task must not run. Intake should skip execute when workflow_dispatch runs on a non-base ref.

### Acceptance Criteria

- Production parse-spec succeeds.
- Production execute is skipped.

### Validation

```text
python -c "raise SystemExit('dispatch-skip case must not reach validation')"
```

# Final Verification

```text
python -c "raise SystemExit('dispatch-skip case must not reach final verification')"
```
