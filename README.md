# md-agent-pipeline

Markdown Task Spec を決定論的 Orchestrator が解釈し、OpenAI Codex CLI へ実装を委譲するための基盤です。

現在は Phase 1（Foundation）までです。Codex 実行、GitHub Actions、PR、CodeRabbit は未実装です。

## Language

Orchestrator は **Python 3.11+** です。

このリポジトリに既存の application stack はなかったため、GitHub Actions から呼び出す決定論的 CLI・JSON/schema 処理向けに Python を選びました。テストは pytest、lint / format は ruff です。追加 runtime dependency はありません。

## Layout

```text
agent/                 Orchestrator code
  config.json          Shared configuration
  config.py            Config loader
  errors.py            Error categories
  logger.py            JSON Lines event logger
  scripts/             CLI entrypoints (later phases)
  schemas/             Spec / state schemas (later phases)
  prompts/             Codex prompts (later phases)
  tests/
.agent/state/          Orchestrator-owned runtime state
specs/tasks/           Human-owned Task Specs (later phases)
```

## Development

```text
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check agent
python -m ruff format --check agent
```
