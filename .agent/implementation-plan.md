# Implementation Plan

## Goal

所定のMarkdown Task Specをbase branchへ配置すると、
GitHub Actions上の決定論的OrchestratorがTaskを解釈し、
OpenAI Codex CLIへ実装を委譲し、
Scope Check・Validation・Repair・Commit・PR・CodeRabbitレビュー修正まで進める基盤を構築する。

---

## Phase Map

| Phase | Name | Depends On | Primary Outcome |
|---|---|---|---|
| 1 | Foundation | none | Orchestratorの土台・共通設定・テスト基盤 |
| 2 | Task Spec & Execution State | 1 | MD Spec解析・Schema Validation・State Machine |
| 3 | Codex CLI Runner | 2 | Codexを制限されたImplementation Engineとして起動 |
| 4 | Scope, Validation & Repair | 3 | Git差分制約・Validation・Repair Loop |
| 5 | GitHub Actions Execution | 4 | MD投入からTask単位で安全に実行 |
| 6 | Git, PR, Restart / GitHub Reconciliation & Observability | 5 | Commit/Push/PR・Restart / GitHub Reconciliation・可観測性 |
| 7 | CodeRabbit Review Loop | 6 | 非同期レビュー分類・Codex修正ループ |

---

## Recommended Milestones

### Milestone A — Local Autonomous Core
Phases 1–4

以下がローカルで成立する状態。

```text
Task Spec
  ↓
Parser / Validator
  ↓
State Machine
  ↓
Codex CLI
  ↓
Scope Check
  ↓
Validation
  ↓
Repair
```

### Milestone B — GitHub Autonomous Delivery
Phases 5–6

以下がGitHub上で成立する状態。

```text
MD Push
  ↓
GitHub Actions
  ↓
Codex
  ↓
Validation
  ↓
Commit / Push
  ↓
Pull Request
```

この時点で実用可能なMVPとする。

### Milestone C — Review Automation
Phase 7

```text
CodeRabbit
  ↓
Event
  ↓
Review Collection
  ↓
Classification
  ↓
Policy
  ↓
Codex Repair
```

---

## Status Tracking

この表は人間向けのRoadmapです。
実装状態の最終的なSource of Truthは実Repository、tests、Git historyです。

- [ ] Phase 1 — Foundation
- [ ] Phase 2 — Task Spec & Execution State
- [ ] Phase 3 — Codex CLI Runner
- [ ] Phase 4 — Scope, Validation & Repair
- [ ] Phase 5 — GitHub Actions Execution
- [ ] Phase 6 — Git, PR, Restart / GitHub Reconciliation & Observability
- [ ] Phase 7 — CodeRabbit Review Loop

---

## Global Completion Criteria

全Phase完了時に以下が成立していること。

- Task SpecをMarkdown + YAML Frontmatterで記述できる
- Task Specを通常プログラムでschema validationできる
- Runtime StateがSpecから分離されている
- Codex CLIはImplementation Engineに限定される
- CodexはState/GitHub Control Planeを操作できない
- Scope違反を実際のGit差分から検出できる
- ValidationをOrchestratorが実行する
- Repair回数をOrchestratorが制御する
- 同一Taskの並行実行を防げる
- GitHub Actions再実行はwork unit単位で最初から行い、既存PRは同一work unitの場合のみ再利用する
- Final Verification後だけPRを作成する
- CodeRabbitを非同期イベントとして処理する
- Review Classification後に決定論的Policyを通す
- Secretとwrite authorityを必要最小限に分離する
- Workflow Summary / logsから状態を追跡できる
