# md-agent-pipeline

Markdown Task Spec を決定論的 Orchestrator が解釈し、OpenAI Codex CLI へ実装を委譲するための基盤です。

現在は Phase 6（Git, PR, Restart / GitHub Reconciliation & Observability）までです。CodeRabbit レビューループは未実装です。

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
  gitwrite.py / github_api.py / reconcile.py / delivery.py
  tests/
.github/workflows/     Task Spec intake, execute, commit/PR deliver
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

### Phase 6 — Git / PR / Observability

通常のIntegration TestではFake CodexとFake GitHubを使用し、APIコストやネットワーク依存を発生させません。実Git（一時repository）でCommit / Push / PR / Reconciliationを検証します。Real CodexとReal GitHub Actionsは起動しません。

```bash
python agent/integration-tests/git-pr-observability/run.py
```

### Phase 5–6 — Real GitHub E2E smoke

本番 `agent-execute.yml` をそのまま使い、Real Codex と Real GitHub 上で Commit / Push / PR 作成と、既存 `workflow_dispatch` による PR reuse を1シナリオで確認します。E2E専用workflowやintake例外は追加しません。Task Spec の `base_branch` は temporary branch（`e2e/phase6-*`）自身です。repository default へは commit しません。

`gh` には対象repositoryについて次が必要です。

- **Contents: write** — temporary base / feature branch の push と delete
- **Pull requests: write** — PR の read / close
- **Actions: write** — run の read / dispatch / cancel

Production 側は既存 Phase 6 Manual Setup（Secret `CODEX_API_KEY`、Actions からの PR 作成許可、workflow active）があれば足しません。Harness は `CODEX_API_KEY` を読みません。

```bash
python -m pip install -r agent/integration-tests/github-pr-e2e/requirements.txt
python agent/integration-tests/github-pr-e2e/self_test.py
python agent/integration-tests/github-pr-e2e/run.py --repo OWNER/REPO
```

調査のため PR / branch を残す場合は `--keep-resources` を付けます。省略時は assertion 後に E2E PR を close し、target branch と source branch を delete します。詳細は `agent/integration-tests/github-pr-e2e/README.md` です。

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
python agent/scripts/run-work-unit.py --spec specs/tasks/example-task.md --report-dir /tmp/agent-report
python agent/scripts/deliver.py --spec specs/tasks/example-task.md --report-dir /tmp/agent-report
python agent/scripts/prepare-intake.py --event-name workflow_dispatch --ref-name main --sha HEAD --spec-path specs/tasks/example-task.md
python agent/scripts/prepare-execute.py --spec specs/tasks/example-task.md
```

GitHub Actions は `.github/workflows/agent-execute.yml` です。`specs/tasks/**/*.md` の push、または `workflow_dispatch` の `spec_path` 入力で起動します。Invalid Spec は workflow を FAIL します。feature branch（spec の `base_branch` 以外）への push は SUCCESS し execute を skip します。同一 `task_id` は execute 完了まで再 push しない運用です。

execute job（`contents: read`）は checkout → Python / Node → 依存 install → `openai/codex-action` による sandbox bootstrap → `run-work-unit.py` の順です。Action は prompt なしで GitHub-hosted runner 上の `workspace-write` sandbox を通す準備だけをし、Orchestrator の代替にはしません。リポジトリ Secret `CODEX_API_KEY` は execute job の Orchestrator step にだけ渡し、Action と deliver job には渡しません。

deliver job（`contents: write` / `pull-requests: write` / `issues: write`）は report と Spec の照合、`patch_sha256`、既存 PR / branch の Reconciliation、`HEAD == base_sha`、clean tree、patch 適用、実差分の Scope（`.agent/state/**` を除外しない）、manifest、Final Verification の順で、すべて通過したときだけ Commit / Push / PR を行います。Codex は Git write を実行しません。実行中の Execution State は runner 上で ephemeral です。workflow 再実行は Task 途中から再開せず、Codex 作業は最初からやり直します。deliver は既存 PR を **同一 work unit**（`spec_id` / target branch / base branch と PR marker）と確認できたときだけ再利用します。reuse 時は patch 再適用も Final Verification 再実行もしません。新規 PR を作っていないため `PR_CREATED` event も出しません（`DeliveryResult.outcome` は CLI 互換のため `PR_CREATED` のままです）。同じ branch の PR があるだけでは再利用せず Escalate します。`.agent/state/*.json` は Resume State ではなく、MVP では Git へ commit しません。Codex / patch が `.agent/state/**` を変更した場合は Scope Violation です。

## Manual setup required

Phase 6 の実 GitHub 実行には人間側の設定が必要です。

- **GitHub Secrets**: Repository Secret `CODEX_API_KEY`（execute の Orchestrator step のみ）
- **Actions permissions**: workflow は default `contents: read`。deliver job だけ `contents: write` / `pull-requests: write` / `issues: write`
- **Allow GitHub Actions to create and approve pull requests**: repository Settings → Actions → General → Workflow permissions で有効化しないと `GITHUB_TOKEN` は PR を作れません
- **branch protection**: base branch の protection は維持してよい。feature branch への Orchestrator push を妨げないこと。force push は使わない
- labels: `agent:ready` / `agent:escalated` / `agent:failed` は未作成なら Deliver が API で作成する（`issues: write`）。`agent:running` は execute に write を足さないため適用しない。`agent:review` は Phase 7
- **Codex authentication**: `CODEX_API_KEY` のみ。deliver / Git / Validation へは渡さない
- **optional notification target**: `agent/config.json` の `notification.mention`。未設定時にユーザー名を生成しない

Codex CLI は公式 `@openai/codex@0.147.0` を pin します。本番の API 認証は Orchestrator が `CODEX_API_KEY` を Codex subprocess にだけ渡す経路です。Action の placeholder `openai-api-key` は sandbox bootstrap 用で、この認証経路ではありません。MVP ではモデルも `agent/config.json` の `codex.model`で Repository 側に明示固定し、ローカルと CI で同じ値を使います。`~/.codex` などのユーザー設定や CLI 暗黙デフォルトには依存しません。
