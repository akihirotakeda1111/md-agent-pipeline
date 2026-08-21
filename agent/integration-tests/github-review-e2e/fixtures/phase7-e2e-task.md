---
schema_version: 1
id: {{TASK_ID}}
title: Phase 7 Real GitHub review E2E {{UNIQUE_ID}}
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
review_attempt_limit: 3
---
# Objective

Add one small, dependency-free Python utility at `{{GENERATED_FILE}}`.

# Non-Goals

- Do not modify application infrastructure or orchestration.
- Do not modify any file except `{{GENERATED_FILE}}`.
- Do not deploy, merge, or access external infrastructure.

# Forbidden Actions

- Do not run Git write commands.
- Do not modify `.agent/state/**`, `.github/**`, `agent/**`, or `specs/**`.
- Do not run Terraform, migrations, deployment, AWS, or database commands.

# Architecture Invariants

- `normalize_csv` is deterministic and has no external side effects.
- Codex may edit only the explicitly allowed E2E file.
- Commit, push, PR, review policy, and convergence remain Production Orchestrator responsibilities.

# Tasks

## create-normalizer: Implement the isolated CSV normalizer

### Requirement

Create `{{GENERATED_FILE}}` with a function:

```python
def normalize_csv(value: str) -> tuple[str, ...]: ...
```

The function must split on commas, trim surrounding whitespace, discard empty items,
lowercase each item, and remove duplicates while preserving first-seen order.

### Acceptance Criteria

- `normalize_csv(" Alpha, beta, ALPHA, , Gamma ")` returns `("alpha", "beta", "gamma")`.
- `normalize_csv("")` returns `()`.
- The implementation uses only the Python standard library.
- No other file is changed by this Task.

### Validation

Run:

```text
python -c "import runpy; f=runpy.run_path('{{GENERATED_FILE}}')['normalize_csv']; assert f(' Alpha, beta, ALPHA, , Gamma ') == ('alpha', 'beta', 'gamma'); assert f('') == ()"
```

# Final Verification

Run:

```text
python -c "import runpy; f=runpy.run_path('{{GENERATED_FILE}}')['normalize_csv']; assert f('a,A,b,a') == ('a', 'b'); assert f(' , ') == (); assert f('x') == ('x',)"
```
