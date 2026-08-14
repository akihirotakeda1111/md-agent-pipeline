# Bootstrap — MD駆動自律開発基盤の段階実装

## Purpose

この文書は、Cursor等のCoding Agentがこのリポジトリ上で
「MD駆動 + Codex CLI + GitHub Actions」の自律開発基盤を段階的に実装するための実行プロトコルです。

この文書自体は実装仕様ではありません。
実装仕様は `.agent/contracts/` と `.agent/phases/` に分離されています。

---

## Source of Truth

実装前に、以下を必ずこの順番で読んでください。

1. `.agent/contracts/security.md`
2. `.agent/contracts/invariants.md`
3. `.agent/contracts/architecture.md`
4. `.agent/implementation-plan.md`
5. 現在実装する `.agent/phases/XX-*.md`

指示が矛盾した場合の優先順位も上記と同じです。

つまり、Phase固有の指示よりGlobal Contractが優先されます。

---

## Execution Rule

### Default Rule

**1回の実装セッションでは1 Phaseだけを実装してください。**

現在PhaseのDefinition of Doneを満たしても、明示的な指示なしに次Phaseへ進まないでください。

### Phase Selection

ユーザーがPhaseを指定した場合、そのPhaseだけを対象にしてください。

Phase指定がない場合は `.agent/implementation-plan.md` を読み、
依存Phaseが完了している最初の未完了Phaseを選択してください。

ただし、Git・テスト・実装状態とMarkdown上のStatusが矛盾する場合は、
Markdownだけを信頼せず実Repositoryを調査してください。

---

## Required Workflow

各Phaseで以下を実行してください。

1. Global Contractsを読む。
2. Current Phaseを読む。
3. Repositoryの現在状態を調査する。
4. 既存実装・CI・言語・package manager・テスト方式を把握する。
5. Current Phaseとの差分を整理する。
6. Current PhaseのAllowed Changes内だけで実装する。
7. Phase固有のValidationを実行する。
8. Repository全体に必要なlint/testがあれば実行する。
9. `git diff` を確認し、意図しない変更がないことを確認する。
10. Definition of Doneを満たしたか判定する。
11. 結果を報告して停止する。

---

## Do Not Auto-Advance

以下は禁止です。

- 複数Phaseを一度にまとめて実装する
- Current PhaseのDoD未達のまま次Phaseを実装する
- 後続Phaseで必要になるという理由だけで先回り実装する
- Phase 7のためにPhase 3でCodeRabbit固有コードを入れる
- 「将来便利そう」という理由で汎用frameworkを追加する

必要なinterfaceだけ先に定義することは許可しますが、
後続Phaseのbusiness logicを先取りしないでください。

---

## External Tool Verification

GitHub Actions、Codex CLI、OpenAI API、CodeRabbit等の
外部ツールの現在仕様を推測してはいけません。

以下を特に幻覚しないでください。

- 存在しないCLI option
- 存在しないnpm package
- 存在しないGitHub Actions context
- 存在しないGitHub event
- 存在しないOpenAI model
- 存在しないCodeRabbit API

Current Phaseで外部仕様が必要な場合は、
**公式ドキュメントをSource of Truthとして確認してから実装してください。**

---

## Implementation Style

- 既存Repositoryのlanguage / toolingを優先する
- 新規dependencyは必要最小限
- small functions / testable modulesを優先
- LLMに通常コードで解決できる仕事を任せない
- structured dataはparser / schemaで扱う
- shell text parsingへの過度な依存を避ける
- errorを握りつぶさない
- exit codeを明示的に扱う
- security-sensitive codeにはfail-closedを優先する

---

## Completion Report

各Phase終了時、以下だけを簡潔に報告してください。

### Implemented
変更した主要ファイルと実装内容。

### Validation
実行したcommandとPASS / FAIL。

### Deferred
意図的に後続Phaseへ残したもの。

### Blockers
人間判断・Secret・外部設定など未解決事項。

### Phase Result
`COMPLETE` / `INCOMPLETE` / `BLOCKED`

`COMPLETE` であっても次Phaseへ自動で進まないでください。
