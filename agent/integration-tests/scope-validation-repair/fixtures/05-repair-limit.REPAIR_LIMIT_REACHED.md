---
schema_version: 1
id: phase4-repair-limit
title: Phase 4 repair limit
status: PENDING
base_branch: main
target_branch: test/phase4-repair-limit
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 2
review_attempt_limit: 0
---
# Objective

Exercise the real Phase 2 through Phase 4 pipeline for repair limit.

# Non-Goals

Do not implement or invoke GitHub Actions, branch creation, commit, push, or pull requests.

# Forbidden Actions

- Do not weaken validation or edit this Task Spec.
- Do not modify files outside allowed_paths.
- Do not perform Git write operations.

# Architecture Invariants

The orchestrator owns scope checks, validation, retry decisions, and terminal state. Codex output is not proof of success.

# Tasks

## implement: Produce the fixture result

depends_on: []

### Requirement

Fixture marker `CASE_05_LIMIT`. Make the smallest implementation change needed by this case.

### Acceptance Criteria

- The orchestrator enforces allowed and forbidden paths before validation.
- Failure handling follows the configured bounded repair policy.

### Validation

```text
python validate.py
```

# Final Verification

```text
python final.py
```

