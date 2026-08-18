---
schema_version: 1
id: {{TASK_ID}}
title: Phase 5-6 Real GitHub E2E {{UNIQUE_ID}}
status: PENDING
base_branch: {{BASE_BRANCH}}
target_branch: {{TARGET_BRANCH}}
allowed_paths:
  - {{GENERATED_FILE}}
forbidden_paths:
  - .agent/state/**
  - .github/**
  - agent/**
  - specs/**
repair_attempt_limit: 1
review_attempt_limit: 0
---
# Objective

Create `{{GENERATED_FILE}}` with exactly this single line:

```text
{{GENERATED_CONTENT}}
```

# Non-Goals

- Do not modify application logic.
- Do not modify any other file.
- Do not deploy or access external infrastructure.

# Forbidden Actions

- Do not run Git write commands.
- Do not modify `.agent/state/**`, `.github/**`, `agent/**`, or `specs/**`.
- Do not run Terraform, migrations, deployment, AWS, or database commands.

# Architecture Invariants

- Codex only changes the explicitly allowed E2E file.
- Commit, push, and Pull Request creation remain Production deliver responsibilities.

# Tasks

## create-e2e-file: Create the isolated E2E marker file

### Requirement

Create `{{GENERATED_FILE}}` containing exactly `{{GENERATED_CONTENT}}` followed by one newline.

### Acceptance Criteria

- The file exists.
- The UTF-8 file content is exactly the required single line plus one newline.
- No other file is changed by this Task.

### Validation

Run:

```text
python -c "from pathlib import Path; p=Path('{{GENERATED_FILE}}'); assert p.read_text(encoding='utf-8') == '{{GENERATED_CONTENT}}\n'"
```

# Final Verification

Run:

```text
python -c "from pathlib import Path; p=Path('{{GENERATED_FILE}}'); assert p.is_file(); assert p.read_text(encoding='utf-8') == '{{GENERATED_CONTENT}}\n'"
```
