# Orchestrator (`agent/`)

Phase 6 まで: Task Spec 解析、Execution State、公式 Codex CLI、Scope Enforcement、Validation、bounded Repair、GitHub Actions、Commit / Push / PR、Restart / GitHub Reconciliation、Observability。

CodeRabbit レビューループは未実装です。

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
| `scripts/run-task.py` | Codex → Scope → Validation → Repair（1 Task） |
| `intake.py` | GHA parse-spec / loop prevention / history / execution guard |
| `scripts/prepare-intake.py` | Spec parse と GITHUB_OUTPUT。Invalid Spec は非ゼロ終了 |
| `scripts/prepare-execute.py` | Git history と Execution State guard |
| `scripts/run-work-unit.py` | 全 Task + Final Verification。Git write しない |
| `scripts/deliver.py` | Commit / Push / PR / labels / summary |
| `gitwrite.py` | Orchestrator の branch / commit / push（force 禁止） |
| `github_api.py` | 公式 GitHub REST（PR / labels / issues） |
| `reconcile.py` | execute の ephemeral execution control と deliver の durable GitHub PR reconciliation。GHA 再実行は State / Git / PR を Resume に使わず最初から |
| `delivery.py` / `workunit.py` | write job と execute report |
| `events.py` / `summary.py` / `notify.py` | JSONL events、job summary、escalation notice |
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
- Codex subprocess の env には `GITHUB_TOKEN` / `OPENAI_API_KEY` を渡さない
- GitHub Actions の `openai/codex-action` 入力 `openai-api-key` は上記の env とは別物。sandbox bootstrap 用の placeholder であり、`OPENAI_API_KEY` を Codex へ渡すことではない
- `--full-auto` は deprecated のため使わない
- Git commit / push / PR は Orchestrator の deliver job が行う。Codex subprocess には GitHub write token を渡さない

### Model (MVP)

MVP では Codex モデルを Repository の `agent/config.json`（`codex.model`）で明示固定します。ローカルと GitHub Actions は同じ Repository Config を使い、CLI の暗黙デフォルトや `~/.codex` 等のユーザー設定には依存しません。`ignore_user_config: true` を維持します。目的はローカルと CI の再現性です。

Task 単位のモデル切替、`allowed_models`、model profile、環境変数による override は MVP 対象外です（Deferred）。将来必要なら Repository default + override 方式へ拡張できます。

## Working-tree policy

Codex 実行前に uncommitted change がある場合、agent 由来差分と区別できないため **fail closed**（`DIRTY_WORKTREE`）です。同一 Work Unit で先行 Task が残した未 commit 差分だけは許可します。Final Verification 通過後、deliver job が Commit / Push / PR します。

## Scope

実際の Git 差分（tracked + untracked + rename/delete + gitignored の `.agent/state/**`）を `allowed_paths` / `forbidden_paths` と照合します。1件でも違反があれば `SCOPE_VIOLATION` とし、Task completed にしません。execute の `run_task_cycle()` は Orchestrator 自身の `.agent/state/{spec.id}.json` だけ scope 対象から外します。deliver は patch 適用後の実差分を再検査し、`.agent/state/**` を除外しません。patch から `.agent/state/**` を落とさないので、Codex の漏れを見逃しません。

## GitHub Actions

`.github/workflows/agent-execute.yml` は parse-spec のあと `should_execute == true` のとき execute を開始し、execute の report artifact を deliver job が受け取ります。`valid` は Spec が parse できたかどうか、`should_execute` は execute を開始するかどうかです。Invalid Spec は parse-spec を非ゼロ終了にして workflow を FAIL します。非 base branch の push は SUCCESS し execute / deliver を skip します。execute は `contents: read` と `CODEX_API_KEY` のみ、`persist-credentials: false` です。deliver は `contents: write` / `pull-requests: write` / `issues: write` を持ち、`CODEX_API_KEY` は持ちません。deliver の checkout も `persist-credentials: false` で、`git push` 時だけ Orchestrator の git サブプロセスへ HTTPS 認証を注入します。checkout は `actions/checkout@v7`、`fetch-depth: 0` です。execute は `autonomous-agent-<task_id>` の job-level concurrency（`cancel-in-progress: false`）を使います。同一 `task_id` は実行完了まで再 push しない運用です。`queue: max` は使いません。execute の setup は checkout → Python / Node → 依存 install → `openai/codex-action@v1`（prompt なしの sandbox bootstrap）→ `run-work-unit.py` → artifact upload です。`.agent/state` は ephemeral runtime metadata です。GHA 再実行では Resume に使わず最初からやり直します。ローカルでは同一 workspace の実行中制御に使えます。deliver は同一 work unit の既存 PR だけを再利用し、reuse 時は patch 再適用も Final Verification 再実行もしません。reuse では `PR_CREATED` event を出しません。MVP では `.agent/state/*.json` を commit しません。Phase 6 が適用する label は `agent:ready` / `agent:escalated` / `agent:failed` です。
