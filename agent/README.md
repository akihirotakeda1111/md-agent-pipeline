# Orchestrator (`agent/`)

Phase 1 の基盤です。後続 Phase の parser / state machine / Codex runner はここへ追加します。

## Modules

| Path | Role |
|---|---|
| `config.json` / `config.py` | Task Spec・State・Codex・retry・review・notification・CodeRabbit の設定を1箇所から load |
| `errors.py` | `InvalidInput` / `EnvironmentFailure` / `PolicyViolation` / `EscalationRequired` / `InternalFailure` |
| `logger.py` | `event`, `task_id`, `phase`, `state`, `message`, `timestamp` の JSON Lines |
| `scripts/` | CLI entrypoints（Phase 2 以降） |
| `schemas/` | Task Spec / Execution State schema（Phase 2） |
| `prompts/` | Codex 向け prompt（Phase 3） |
| `tests/` | unit tests |

設定値の placeholder（Codex bin 等）は構造だけ用意し、実行ロジックは持たせません。
