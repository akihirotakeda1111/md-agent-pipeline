# Orchestrator (`agent/`)

Phase 3 まで: Task Spec 解析、Execution State、公式 Codex CLI を制限付き Implementation Engine として起動。

GitHub Actions / Validation loop / Repair / PR / CodeRabbit は未実装です。

## Modules

| Path | Role |
|---|---|
| `config.json` / `config.py` | Task Spec・State・Codex・retry・review・notification・CodeRabbit の設定を1箇所から load |
| `errors.py` | `InvalidInput` / `EnvironmentFailure` / `PolicyViolation` / `EscalationRequired` / `InternalFailure` |
| `logger.py` | `event`, `task_id`, `phase`, `state`, `message`, `timestamp` の JSON Lines |
| `spec.py` | Markdown + YAML Frontmatter の parse と JSON Schema validation |
| `state.py` | Execution State の JSON read/write と明示的 state machine |
| `select.py` | 定義順 + dependency + completedTasks による次 Task 選択 |
| `codex_runner.py` | 公式 `codex exec` の command / env / prompt / subprocess |
| `scripts/parse-spec.py` | Spec を JSON へ変換 |
| `scripts/validate-spec.py` | Spec を Codex なしで検証。不正時は `INVALID_SPEC` |
| `scripts/init-state.py` | `.agent/state/<task-id>.json` を PENDING で作成 |
| `scripts/update-state.py` | 許可された遷移だけを受理して State を更新 |
| `scripts/select-task.py` | 次 Task を決定論的に選択 |
| `scripts/run-codex.py` | Codex runner CLI。State / Git / PR は操作しない |
| `schemas/` | `task-spec.schema.json` / `execution-state.schema.json` |
| `prompts/implementation.md` | Codex 向け implementation contract |
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

## Codex CLI (official)

Source of truth:

- https://github.com/openai/codex
- https://developers.openai.com/codex/noninteractive
- https://developers.openai.com/codex/environment-variables
- https://developers.openai.com/codex/agent-approvals-security

Pinned package: `@openai/codex@0.147.0` (`latest` は追従しない).

```text
npm install -g @openai/codex@0.147.0
```

Runner が組み立てる非対話コマンド（公式フラグのみ）:

```text
codex exec --sandbox workspace-write --output-last-message <file> --json --ignore-user-config -
```

- 認証は subprocess にだけ `CODEX_API_KEY` を渡す
- `GITHUB_TOKEN` / `OPENAI_API_KEY` は渡さない
- `--full-auto` は deprecated のため使わない
- Git commit / push / PR / state update は行わない
