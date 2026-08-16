---
schema_version: 1
id: phase3-add-function
title: Add a source function
status: PENDING
base_branch: main
target_branch: test/phase3-add-function
allowed_paths: [src/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective

Add a small function to an existing source module.

# Non-Goals

Do not refactor or reformat the existing function.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not execute Validation or Final Verification.
- Do not perform Git or GitHub operations.

# Architecture Invariants

The existing `increment` function remains available and unchanged.

# Tasks

## add-double: Add the double function

depends_on: []

### Requirement

Fixture marker `CASE_04_ADD_FUNCTION`. Add `double(value)` to `src/math_utils.py`; it must return `value * 2`.

### Acceptance Criteria

- The existing `increment` function remains unchanged.
- `double(value)` returns `value * 2`.

### Validation

```text
python -c "from pathlib import Path; Path('PHASE3_VALIDATION_MUST_NOT_RUN').write_text('ran')"
```

# Final Verification

```text
python -c "from pathlib import Path; Path('PHASE3_FINAL_VERIFICATION_MUST_NOT_RUN').write_text('ran')"
```
