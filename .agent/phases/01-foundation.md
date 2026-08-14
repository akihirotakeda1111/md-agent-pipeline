# Phase 1 — Foundation

## Objective

後続Phaseを安全に実装できる、最小のOrchestrator基盤を作る。

このPhaseではCodex CLI実行、GitHub Actions、PR作成、CodeRabbit連携は実装しない。

---

## Depends On

なし。

---

## Read First

- `.agent/contracts/security.md`
- `.agent/contracts/invariants.md`
- `.agent/contracts/architecture.md`

---

## Repository Audit

実装前に以下を確認する。

- repository directory structure
- primary languages
- package manager
- test framework
- lint / format commands
- existing CI
- existing `.github/workflows`
- Terraform等infra directory
- existing CodeRabbit config
- existing agent-related files

既存の実装言語・toolingに合わせること。

---

## Deliverables

最低限以下に相当する構造を用意する。

```text
agent/
├── config.*
├── scripts/
├── schemas/
├── prompts/
└── tests/

.agent/
└── state/
```

実Repositoryの慣習に応じて拡張子や配置は調整してよい。

---

## Implementation Requirements

### Common Config

以下を将来保持できるconfig構造を作る。

- Task Spec directory
- State directory
- Codex configuration
- retry defaults
- review defaults
- notification configuration
- CodeRabbit actor configuration

Phase 1では値をすべて実装しなくてもよいが、
後からglobal constantが散らばらない構造にする。

### Structured Logging

最低限以下を出力できる共通logger/helperを用意する。

```text
event
task_id
phase/state
message
timestamp
```

可能ならJSON Linesを利用する。

### Error Types

少なくとも以下を区別可能なerror modelを用意する。

```text
InvalidInput
EnvironmentFailure
PolicyViolation
EscalationRequired
InternalFailure
```

### Test Skeleton

後続のparser / state / policyをunit testできるテスト構造を作る。

---

## Allowed Changes

- `agent/**`
- `.agent/**`
- test/config files needed for the agent platform
- documentation directly required by this Phase

既存Application Sourceを理由なく変更しない。

---

## Forbidden Changes

- Production application architecture変更
- GitHub Actions autonomous workflow実装
- Codex CLI invocation
- Git write automation
- PR creation
- CodeRabbit integration
- new database
- external workflow engine

---

## Acceptance Criteria

- Orchestrator codeの置き場所が確立している
- configを1箇所からloadできる
- structured event logを出力できる
- error categoryを区別できる
- unit testを追加できる
- 後続Phaseが既存Applicationへ密結合しない

---

## Validation

Repositoryのstackに応じて以下相当を実行する。

```text
agent unit tests
agent lint
agent formatter/check
```

新規言語を導入した場合は、その選定理由を明記する。

---

## Definition of Done

- Deliverablesが存在する
- unit test infrastructureが動く
- lint / testがPASSする
- 後続Phaseのbusiness logicを先取りしていない
- 意図しないApplication Source変更がない

---

## Stop Condition

DoDを満たしたら結果を報告して停止する。
Phase 2へ進まない。
