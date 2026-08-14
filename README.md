# md-agent-pipeline

Markdown Task Spec を決定論的 Orchestrator が解釈し、OpenAI Codex CLI へ実装を委譲するための基盤です。

現在は Phase 2（Task Spec & Execution State）までです。Codex 実行、GitHub Actions、PR、CodeRabbit は未実装です。

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
  prompts/             Codex prompts (later phases)
  tests/
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

```text
python agent/integration-tests/task-orchestration/run.py
```

## Spec / state CLIs

```text
python agent/scripts/parse-spec.py specs/tasks/example-task.md
python agent/scripts/validate-spec.py specs/tasks/example-task.md
python agent/scripts/init-state.py --spec specs/tasks/example-task.md
python agent/scripts/update-state.py --task-id phase2-step2 --to RUNNING
python agent/scripts/select-task.py --spec specs/tasks/example-task.md
```
