---
schema_version: 1
id: phase3-multiple-files
title: Modify multiple allowed files
status: PENDING
base_branch: main
target_branch: test/phase3-multiple-files
allowed_paths: [app/**, docs/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective

Apply one selected task across multiple files in allowed scope.

# Non-Goals

Do not split the work into additional runtime tasks.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not execute Validation or Final Verification.
- Do not perform Git or GitHub operations.

# Architecture Invariants

Application state and user documentation describe the same feature state.

# Tasks

## enable-feature: Enable and document the feature

depends_on: []

### Requirement

Fixture marker `CASE_05_MULTIPLE_FILES`. Change `app/feature.txt` from `disabled` to `enabled`, and create `docs/feature.txt` containing `Feature is enabled`, each followed by one newline.

### Acceptance Criteria

- `app/feature.txt` contains `enabled`.
- `docs/feature.txt` contains `Feature is enabled`.

### Validation

```text
python -c "from pathlib import Path; Path('PHASE3_VALIDATION_MUST_NOT_RUN').write_text('ran')"
```

# Final Verification

```text
python -c "from pathlib import Path; Path('PHASE3_FINAL_VERIFICATION_MUST_NOT_RUN').write_text('ran')"
```
