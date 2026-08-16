---
schema_version: 1
id: phase3-no-change
title: Avoid unnecessary workspace changes
status: PENDING
base_branch: main
target_branch: test/phase3-no-change
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective

Leave a workspace unchanged when Acceptance Criteria are already satisfied.

# Non-Goals

Do not rewrite an already-correct file.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not execute Validation or Final Verification.
- Do not perform Git or GitHub operations.

# Architecture Invariants

Idempotent task execution must not produce cosmetic changes.

# Tasks

## ensure-greeting: Ensure the greeting is present

depends_on: []

### Requirement

Fixture marker `CASE_06_NO_CHANGE`. Ensure `app/greeting.txt` contains exactly `Hello from Codex` followed by one newline. If it already does, do not rewrite it or modify any file.

### Acceptance Criteria

- The desired content is present.
- The complete workspace byte snapshot is unchanged.

### Validation

```text
python -c "from pathlib import Path; Path('PHASE3_VALIDATION_MUST_NOT_RUN').write_text('ran')"
```

# Final Verification

```text
python -c "from pathlib import Path; Path('PHASE3_FINAL_VERIFICATION_MUST_NOT_RUN').write_text('ran')"
```
