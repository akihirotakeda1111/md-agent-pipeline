# md-agent-pipeline

Markdown Task Spec を決定論的 Orchestrator が解釈し、OpenAI Codex CLI へ実装を委譲するための基盤です。

現在は Phase 5（GitHub Actions Execution）までです。Commit / Push、PR、Resume、CodeRabbit は未実装です。

## Language

Orchestrator は **Python 3.11+** です。

このリポジトリに既存の application stack はなかったため、GitHub Actions から呼び出す決定論的 CLI・JSON/schema 処理向けに Python を選びました。テストは pytest、lint / format は ruff です。
runtime dependency は PyYAML と jsonschema のみです。

## Layout

```text
agent/                 Orchestrator code
  config.json          Shared configuration
  config.py            Config loader
  errors.py            Error categories
  logger.py            JSON Lines event logger
  spec.py              Task Spec parser / validator
  state.py             Execution State + state machine
  select.py            Deterministic task selection
  scripts/             CLI entrypoints
  schemas/             JSON Schema
  prompts/             Codex implementation / repair prompts
  codex_runner.py      Official `codex exec` runner
  gitutil.py / scope.py / validation.py / classify.py / cycle.py
  tests/
.github/workflows/     Task Spec intake + execute (no commit/PR)
.agent/state/          Orchestrator-owned runtime state
specs/tasks/           Human-owned Task Specs
```

## Development

```text
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check agent
python -m ruff format --check agent
```

## Integration Tests

すべてプロジェクトルートから実行します。

### Phase 2 — Task Orchestration

```bash
python agent/integration-tests/task-orchestration/run.py
```

### Phase 3 — Codex Execution

通常のIntegration TestではFake Codexを使用し、APIコストやネットワーク依存を発生させません。

```bash
python agent/integration-tests/codex-execution/run.py
```

### Phase 3 — Real Codex Smoke Test

実Codex CLIを使用するテストはLinux環境で実行します。Windowsを使用する場合は、Windowsネイティブ環境ではなくWSL上で実行してください。

デフォルトではCase 01（`01-create-file`）だけを実行します。

```bash
RUN_CODEX_SMOKE_TEST=1 python3 agent/integration-tests/codex-execution/run.py --real-codex
```

Real Codex対象の全ケースを実行する場合は、`--all-cases`を指定します。

```bash
RUN_CODEX_SMOKE_TEST=1 python3 agent/integration-tests/codex-execution/run.py --real-codex --all-cases
```

Real Codex Smoke Testには、公式Codex CLIのインストールと認証が必要です。`RUN_CODEX_SMOKE_TEST=1`が設定されていない場合、Real Codexは実行されません。

### Phase 4 — Scope / Validation / Repair

通常のIntegration TestではFake Codexを使用し、APIコストやネットワーク依存を発生させません。実Codex CLI smokeは実装していません。

```bash
python agent/integration-tests/scope-validation-repair/run.py
```

### Phase 5 — GitHub Actions

Contract 検査は本番 `.github/workflows/agent-execute.yml` をローカルで読みます。GitHub は不要です。

```bash
python agent/integration-tests/github-actions/integration/verify_contract.py
```

実 GitHub 結合は本番 `agent-execute.yml` を push / `workflow_dispatch` します。`01-normal-success` だけ Real Codex（Secret `CODEX_API_KEY`）を使います。`gh` には **Contents: write** と **Actions: read/write** が必要です。

```bash
python agent/integration-tests/github-actions/run.py --repo OWNER/REPO
```

## Spec / state CLIs

```text
python agent/scripts/parse-spec.py specs/tasks/example-task.md
python agent/scripts/validate-spec.py specs/tasks/example-task.md
python agent/scripts/init-state.py --spec specs/tasks/example-task.md
python agent/scripts/update-state.py --task-id phase2-step2 --to RUNNING
python agent/scripts/select-task.py --spec specs/tasks/example-task.md
python agent/scripts/run-codex.py --spec specs/tasks/example-task.md --task task-1
python agent/scripts/check-scope.py --spec specs/tasks/example-task.md
python agent/scripts/run-validation.py --spec specs/tasks/example-task.md --task task-1
python agent/scripts/run-task.py --spec specs/tasks/example-task.md
python agent/scripts/prepare-intake.py --event-name workflow_dispatch --ref-name main --sha HEAD --spec-path specs/tasks/example-task.md
python agent/scripts/prepare-execute.py --spec specs/tasks/example-task.md
```

GitHub Actions は `.github/workflows/agent-execute.yml` です。`specs/tasks/**/*.md` の push、または `workflow_dispatch` の `spec_path` 入力で起動します。Invalid Spec は workflow を FAIL します。feature branch（spec の `base_branch` 以外）への push は SUCCESS し execute を skip します。同一 `task_id` は execute 完了まで再 push しない運用です。execute job は checkout → Python / Node → 依存 install → `openai/codex-action` による sandbox bootstrap → `run-task.py` の順です。Action は prompt なしで GitHub-hosted runner 上の `workspace-write` sandbox を通す準備だけをし、Orchestrator の代替にはしません。リポジトリ Secret `CODEX_API_KEY` は execute job の Orchestrator step にだけ渡し、Action には渡しません。

Codex CLI は公式 `@openai/codex@0.147.0` を pin します。認証は `CODEX_API_KEY` をそのプロセスにだけ渡します。MVP ではモデルも `agent/config.json` の `codex.model`で Repository 側に明示固定し、ローカルと CI で同じ値を使います。`~/.codex` などのユーザー設定や CLI 暗黙デフォルトには依存しません。
