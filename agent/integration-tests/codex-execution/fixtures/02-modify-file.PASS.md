---
schema_version: 1
id: phase3-modify-file
title: Modify an existing file
status: PENDING
base_branch: main
target_branch: test/phase3-modify-file
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective

Modify a specific part of an existing application file.

# Non-Goals

Do not rewrite unrelated lines or create additional files.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not execute Validation or Final Verification.
- Do not perform Git or GitHub operations.

# Architecture Invariants

Existing unrelated content must be preserved.

# Tasks

## update-status: Update the message status

depends_on: []

### Requirement

Fixture marker `CASE_02_MODIFY_FILE`. In `app/message.txt`, change only `status=old` to `status=new`.

### Acceptance Criteria

- The status line is `status=new`.
- The existing `keep=this line` line is unchanged.

### Validation

```text
python -c "from pathlib import Path; Path('PHASE3_VALIDATION_MUST_NOT_RUN').write_text('ran')"
```

# Final Verification

```text
python -c "from pathlib import Path; Path('PHASE3_FINAL_VERIFICATION_MUST_NOT_RUN').write_text('ran')"
```
