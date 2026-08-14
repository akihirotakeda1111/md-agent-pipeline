# Phase 2 — Task Spec & Execution State

## Objective

Markdown + YAML FrontmatterのTask Specを機械解析し、
Schema Validation、Task選択、Execution State、State Machineを
LLMなしで扱えるようにする。

---

## Depends On

Phase 1 — Foundation

---

## Deliverables

最低限:

```text
agent/schemas/task-spec.schema.*
agent/schemas/execution-state.schema.*
agent/scripts/parse-spec.*
agent/scripts/validate-spec.*
agent/scripts/init-state.*
agent/scripts/update-state.*
agent/scripts/select-task.*
agent/tests/...
specs/tasks/TEMPLATE.md
specs/tasks/example-task.md
```

---

## Task Spec Format

MetadataはYAML Frontmatterに限定する。

最低限:

```yaml
---
schema_version: 1
id: phase2-step2
title: Idempotent Worker
status: PENDING
base_branch: dev
target_branch: feature/phase2-worker

allowed_paths:
  - worker/**
  - infra/terraform/**

forbidden_paths:
  - specs/**
  - .agent/**
  - agent/**
  - .github/**

repair_attempt_limit: 3
review_attempt_limit: 3
---
```

Markdown本文には最低限:

```text
Objective
Non-Goals
Forbidden Actions
Architecture Invariants
Tasks
  Requirement
  Acceptance Criteria
  Validation
Final Verification
```

を持つ。

---

## Validation Requirements

Codexを起動せず、通常プログラムで以下を検証する。

- Frontmatter parse成功
- supported schema_version
- id
- title
- status
- base_branch
- target_branch
- allowed_paths
- repair_attempt_limit
- review_attempt_limit
- Objective
- 1つ以上のTask
- 各TaskのRequirement
- 各TaskのAcceptance Criteria
- 各TaskのValidation
- Final Verification

不正なSpecは `INVALID_SPEC` とする。

---

## Execution State

Runtime StateはTask Specから分離する。

```text
.agent/state/<task-id>.json
```

例:

```json
{
  "schemaVersion": 1,
  "taskId": "phase2-step2",
  "state": "VALIDATING",
  "currentTask": "task-2",
  "completedTasks": ["task-1"],
  "repairAttempts": 1,
  "reviewAttempts": 0,
  "lastValidation": "cargo test test_heartbeat_loop",
  "lastResult": "FAILED",
  "branch": "feature/phase2-worker",
  "pullRequest": null
}
```

JSON parserを使用する。
regexやLLMでStateを更新しない。

---

## State Machine

最低限:

```text
PENDING
RUNNING
IMPLEMENTING
VALIDATING
TASK_COMPLETED
FINAL_VALIDATING
PR_CREATED
IN_REVIEW
READY_FOR_HUMAN
COMPLETED

INVALID_SPEC
SCOPE_VIOLATION
FAILED
ESCALATED
```

明示的に許可された遷移だけを受理する。

例:

```text
PENDING -> RUNNING
RUNNING -> IMPLEMENTING
IMPLEMENTING -> VALIDATING
VALIDATING -> TASK_COMPLETED
TASK_COMPLETED -> IMPLEMENTING
TASK_COMPLETED -> FINAL_VALIDATING
FINAL_VALIDATING -> PR_CREATED
PR_CREATED -> IN_REVIEW
IN_REVIEW -> READY_FOR_HUMAN
READY_FOR_HUMAN -> COMPLETED
```

異常遷移も明示する。

---

## Task Selection

Task選択はLLMに任せない。

- 定義順
- dependency
- completedTasks

から決定論的に次Taskを選ぶ。

依存Task未完了なら選択不可。

---

## Allowed Changes

- `agent/**`
- `.agent/**`
- `specs/tasks/**`
- related tests/docs

---

## Forbidden Changes

- Codex CLI runner
- GitHub Actions autonomous workflow
- Git commit / push automation
- PR creation
- CodeRabbit
- Application Source

---

## Tests

最低限:

- valid spec
- invalid YAML
- missing frontmatter
- missing required property
- unsupported schema_version
- zero tasks
- Task missing Acceptance Criteria
- state init
- state read/write
- valid transition
- invalid transition
- next task selection
- dependency blocked task

---

## Definition of Done

- Example Taskをparseできる
- Invalid SpecをCodexなしでrejectできる
- State JSONを安全にread/writeできる
- State Machineが不正遷移を拒否する
- 次Taskを決定論的に選べる
- unit testsがPASSする

---

## Stop Condition

DoD後、Phase 3へ進まず停止する。
