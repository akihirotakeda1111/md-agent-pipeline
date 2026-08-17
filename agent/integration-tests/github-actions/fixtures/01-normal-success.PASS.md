---
schema_version: 1
id: __TASK_ID__
title: Phase 5 GitHub Actions normal smoke
status: PENDING
base_branch: __BASE_BRANCH__
target_branch: __TARGET_BRANCH__
allowed_paths: [app/**]
forbidden_paths: [specs/**, .agent/**, agent/**, .github/**]
repair_attempt_limit: 1
review_attempt_limit: 0
---
# Objective

Write a single marker file so production Agent Execute can complete one local task cycle on GitHub Actions.

# Non-Goals

- Do not create branches, commit, push, or open pull requests.
- Do not edit files outside allowed_paths.
- Do not change Orchestrator, workflow, or Task Spec files.

# Forbidden Actions

- Do not run git write operations.
- Do not weaken validation.
- Do not modify files outside `app/**`.

# Architecture Invariants

The Orchestrator owns scope checks, validation, and terminal state. Codex output is not proof of success.

# Tasks

## write-result: Write the PASS marker

depends_on: []

### Requirement

Create `app/result.txt` containing exactly `PASS` followed by a newline.

### Acceptance Criteria

- `app/result.txt` exists.
- File contents are `PASS` plus a trailing newline.
- No other paths are modified.

### Validation

```text
python -c "from pathlib import Path; assert Path('app/result.txt').read_text(encoding='utf-8') == 'PASS\n'"
```

# Final Verification

```text
python -c "from pathlib import Path; assert Path('app/result.txt').read_text(encoding='utf-8') == 'PASS\n'"
```
