# Orchestrator (`agent/`)

Phase 4 まで: Task Spec 解析、Execution State、公式 Codex CLI、Scope Enforcement、Validation、bounded Repair。

GitHub Actions / Commit / Push / PR / CodeRabbit は未実装です。

## Modules

| Path | Role |
|---|---|
| `config.json` / `config.py` | Task Spec・State・Codex・retry・validation・review・notification・CodeRabbit の設定を1箇所から load |
| `errors.py` | `InvalidInput` / `EnvironmentFailure` / `PolicyViolation` / `EscalationRequired` / `InternalFailure` |
| `logger.py` | `event`, `task_id`, `phase`, `state`, `message`, `timestamp` の JSON Lines |
| `spec.py` | Markdown + YAML Frontmatter の parse と JSON Schema validation |
| `state.py` | Execution State の JSON read/write と明示的 state machine |
| `select.py` | 定義順 + dependency + completedTasks による次 Task 選択 |
| `codex_runner.py` | 公式 `codex exec` の command / env / prompt / subprocess |
| `gitutil.py` | BASE_SHA と working-tree diff（commit/push しない） |
| `scope.py` | allowed/forbidden path の機械検証 |
| `validation.py` | Validation command の安全実行 |
| `classify.py` | AGENT_REPAIRABLE / ENVIRONMENT_FAILURE / ESCALATION_REQUIRED |
| `repair.py` / `cycle.py` | bounded repair と local task cycle |
| `scripts/parse-spec.py` | Spec を JSON へ変換 |
| `scripts/validate-spec.py` | Spec を Codex なしで検証。不正時は `INVALID_SPEC` |
| `scripts/init-state.py` | `.agent/state/<task-id>.json` を PENDING で作成 |
| `scripts/update-state.py` | 許可された遷移だけを受理して State を更新 |
| `scripts/select-task.py` | 次 Task を決定論的に選択 |
| `scripts/run-codex.py` | Codex runner CLI。State / Git / PR は操作しない |
| `scripts/check-scope.py` | 差分の Scope Check |
| `scripts/run-validation.py` | Orchestrator による Validation |
| `scripts/run-task.py` | Codex → Scope → Validation → Repair |
| `schemas/` | `task-spec.schema.json` / `execution-state.schema.json` |
| `prompts/implementation.md` / `prompts/repair.md` | Codex 向け contract |
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

## Working-tree policy

Codex 実行前に uncommitted change がある場合、agent 由来差分と区別できないため **fail closed**（`DIRTY_WORKTREE`）です。同一 Work Unit で先行 Task が残した未 commit 差分だけは許可します（この Phase は commit しません）。

## Scope

実際の Git 差分（tracked + untracked + rename/delete）を `allowed_paths` / `forbidden_paths` と照合します。1件でも違反があれば `SCOPE_VIOLATION` とし、Task completed にしません。`run_task_cycle()` は Orchestrator 自身の `.agent/state/{spec.id}.json` だけ scope 対象から外します。`.agent/state/**` 全体が Codex 変更から保護されていることまでは保証しません。
