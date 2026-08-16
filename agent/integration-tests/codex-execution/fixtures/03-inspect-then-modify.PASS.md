---
schema_version: 1
id: phase3-inspect-modify
title: Inspect configuration then modify banner
status: PENDING
base_branch: main
target_branch: test/phase3-inspect-modify
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective

Use repository information that is not embedded in the prompt to produce the correct change.

# Non-Goals

Do not modify the configuration source.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not execute Validation or Final Verification.
- Do not guess configuration values.

# Architecture Invariants

`config.txt` is read-only input for this task.

# Tasks

## update-banner: Apply the repository prefix

depends_on: []

### Requirement

Fixture marker `CASE_03_INSPECT_THEN_MODIFY`. Read the `prefix` value from repository file `config.txt` and set `app/banner.txt` to `<prefix>-ready` followed by one newline. The prefix value is intentionally not stated in this Spec.

### Acceptance Criteria

- `app/banner.txt` uses the value actually read from `config.txt`.
- `config.txt` remains unchanged.

### Validation

```text
python -c "from pathlib import Path; Path('PHASE3_VALIDATION_MUST_NOT_RUN').write_text('ran')"
```

# Final Verification

```text
python -c "from pathlib import Path; Path('PHASE3_FINAL_VERIFICATION_MUST_NOT_RUN').write_text('ran')"
```
