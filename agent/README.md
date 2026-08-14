# Orchestrator (`agent/`)

Phase 2 まで: Task Spec の機械解析、Schema Validation、Execution State、State Machine、決定論的 Task 選択。

Codex runner / GitHub Actions / PR / CodeRabbit は未実装です。

## Modules

| Path | Role |
|---|---|
| `config.json` / `config.py` | Task Spec・State・Codex・retry・review・notification・CodeRabbit の設定を1箇所から load |
| `errors.py` | `InvalidInput` / `EnvironmentFailure` / `PolicyViolation` / `EscalationRequired` / `InternalFailure` |
| `logger.py` | `event`, `task_id`, `phase`, `state`, `message`, `timestamp` の JSON Lines |
| `spec.py` | Markdown + YAML Frontmatter の parse と JSON Schema validation |
| `state.py` | Execution State の JSON read/write と明示的 state machine |
| `select.py` | 定義順 + dependency + completedTasks による次 Task 選択 |
| `scripts/parse-spec.py` | Spec を JSON へ変換 |
| `scripts/validate-spec.py` | Spec を Codex なしで検証。不正時は `INVALID_SPEC` |
| `scripts/init-state.py` | `.agent/state/<task-id>.json` を PENDING で作成 |
| `scripts/update-state.py` | 許可された遷移だけを受理して State を更新 |
| `scripts/select-task.py` | 次 Task を決定論的に選択 |
| `schemas/` | `task-spec.schema.json` / `execution-state.schema.json` |
| `prompts/` | Codex 向け prompt（Phase 3） |
| `tests/` | unit tests |

## Task Spec headings

Markdown 本文は次の H1 を必須とします。

- Objective
- Non-Goals
- Forbidden Actions
- Architecture Invariants
- Tasks
- Final Verification

各 Task は `## <task-id>: title` とし、`### Requirement` / `### Acceptance Criteria` / `### Validation` を必須とします。依存は Task 本文先頭の `depends_on: task-1, task-2` で宣言します。
