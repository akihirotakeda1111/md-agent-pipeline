---
schema_version: 1
id: phase3-create-file
title: Create greeting file
status: PENDING
base_branch: main
target_branch: test/phase3-create-file
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 0
review_attempt_limit: 0
---
# Objective

Create a new application file through the Phase 3 Codex Runner.

# Non-Goals

Do not add tests, CI, state transitions beyond task selection, or Git operations.

# Forbidden Actions

- Do not edit files outside `allowed_paths`.
- Do not execute Validation or Final Verification.
- Do not modify Task Specs, Execution State, agent infrastructure, or GitHub workflows.

# Architecture Invariants

Runtime state remains separate from this Task Spec.

# Tasks

## create-greeting: Create the greeting

depends_on: []

### Requirement

Fixture marker `CASE_01_CREATE_FILE`. Create `app/greeting.txt` with exactly `Hello from Codex` followed by one newline.

### Acceptance Criteria

- `app/greeting.txt` exists.
- Its complete content is exactly `Hello from Codex` followed by one newline.

### Validation

```text
python -c "from pathlib import Path; Path('PHASE3_VALIDATION_MUST_NOT_RUN').write_text('ran')"
```

# Final Verification

```text
python -c "from pathlib import Path; Path('PHASE3_FINAL_VERIFICATION_MUST_NOT_RUN').write_text('ran')"
```
