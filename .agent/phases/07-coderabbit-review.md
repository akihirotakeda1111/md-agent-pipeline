# Phase 7 — CodeRabbit Asynchronous Review Loop

## Objective

PR作成後に非同期で発生するCodeRabbit feedbackを検知し、
未処理レビューだけを収集・分類・Policy評価し、
安全な指摘だけをCodexへ修正させる。

---

## Depends On

Phase 6 — Git, Pull Request, Restart / GitHub Reconciliation & Observability

---

## Architecture

```text
CodeRabbit
  ↓
GitHub Event
  ↓
agent-review.yml
  ↓
Deterministic Pre-filter
  ↓
Review Collection
  ↓
Semantic Classifier
  ↓
Structured Output Validation
  ↓
Deterministic Policy Engine
  ↓
ACTIONABLE?
  ├─ no -> ignore/escalate
  └─ yes
       ↓
     Codex Fix
       ↓
     Scope Check
       ↓
     Validation
       ↓
     Commit / Push
```

---

## Async Requirement

`agent-execute.yml` 内でCodeRabbit完了を待たない。

禁止:

- arbitrary sleep
- indefinite polling
- execute workflow内でreview完了待ち

別workflow:

```text
.github/workflows/agent-review.yml
```

を使用する。

---

## Event Verification

Phase 7 Agent Review の本処理は、CodeRabbit レビューが terminal になったときだけ起動する。

正式な wake-up:

```text
check_run  (types: completed)
status     (pending は prepare で skip)
```

コメント系は起動条件から除外する。

```text
issue_comment
pull_request_review_comment
pull_request_review
```

`@coderabbitai full review` は CodeRabbit への指示であり、Agent Review の wake-up ではない。
full review 受付応答、summary / walkthrough、途中レビューコメントでも起動しない。

terminal 判定はコメント本文（例: `Full review finished.`）を解析しない。
Check Run の `status=completed` と commit status の非 pending `state` を使う。

Event payload は wake-up signal のみ。起動後に GitHub API から current HEAD の
review / comment と CodeRabbit terminal evidence を再取得し、その後に
Classifier → Policy → Codex / READY / ESCALATED を実行する。

---

## Review Collection

Workflow起動後、GitHub API等から
現在の対象PRに存在するCodeRabbit feedbackを再取得する。

通常コードで最低限:

- PR identity
- bot identity
- comment/review ID
- created/updated timestamp
- path/line if present
- already processed
- duplicate
- current PR head

を扱う。

CodeRabbit actor名はconfigurableにする。

---

## Deterministic Pre-filter

LLMを呼ぶ前に通常コードで判定する。

- CodeRabbit由来か
- 対象PRか
- 未処理か
- duplicateでないか
- obsolete head向けでないか
- file pathが存在するか
- obvious forbidden pathか

機械判定できるものにLLMを使わない。

---

## Semantic Classification

自然言語意味分類だけLLM APIを使用してよい。

Enum:

```text
ACTIONABLE
NON_ACTIONABLE
OUT_OF_SCOPE
CONFLICTS_WITH_SPEC
UNCERTAIN
```

必ずStructured Output / JSON Schemaを利用する。

例:

```json
{
  "classification": "ACTIONABLE",
  "confidence": 0.93,
  "reason": "Localized implementation correction.",
  "referencedPaths": [
    "worker/src/sqs/poller.rs"
  ]
}
```

Structured Outputはschema準拠のためのものであり、
分類意味の正しさを保証するものではない。

---

## Classifier Model

configurableにする。

```yaml
review_classifier:
  provider: openai
  model: "<PINNED_MODEL>"
  confidence_threshold: 0.80
```

実装時点で公式OpenAI APIに存在し、
必要なStructured Outputに対応するmodelを確認する。

架空のmodel名を生成しない。

可能ならversion/snapshotをpinする。

---

## Credential Isolation

Classifier用credentialとCodex用credentialを分離する。

例:

```text
CODEX credential
ORCHESTRATOR classifier credential
```

Classifier credentialをCodex subprocessへ渡さない。

Codex credentialをreview classifierへ不要に渡さない。

---

## Deterministic Review Policy

LLM分類を直接execution authorityにしない。

最低限:

```text
ACTIONABLE
+ confidence >= threshold
+ referencedPaths subset of allowed_paths
→ Codex Fix

ACTIONABLE
+ low confidence
→ ESCALATED

NON_ACTIONABLE
→ mark processed / ignore

OUT_OF_SCOPE
→ ESCALATED

CONFLICTS_WITH_SPEC
→ ESCALATED

UNCERTAIN
→ ESCALATED
```

---

## Review Repair Loop

1 Review Attempt:

```text
accepted review set
→ Codex fix
→ Scope Check
→ relevant Validation
→ commit
→ push
```

`review_attempt_limit` を超えない。

CodeRabbitの再レビューは次のeventで処理する。
同一workflowで無期限待機しない。

---

## Codex Review Prompt

最低限:

- original Task Spec
- relevant Current Task
- accepted review comments
- referenced files
- current diff
- allowed scope
- architecture invariants
- validation commands

を渡す。

ReviewがSpecと衝突する場合は修正させない。

---

## Processed Review Tracking

同一comment/reviewを何度も処理しないよう、
Machine-owned Stateにprocessed ID等を保存する。

ただしGitHub上でcommentがeditedされた場合のpolicyも決める。

---

## Security

GitHub event handlingでuntrusted code + Secretsの組み合わせを作らない。

`pull_request_target` 等を利用する場合は、
fork code checkout/executionによるSecret exposureリスクを
公式GitHub security guidanceで確認する。

---

## Observability

追加events:

```text
REVIEW_RECEIVED
REVIEW_FILTERED
REVIEW_COLLECTED
REVIEW_CLASSIFIED
REVIEW_POLICY_APPLIED
REVIEW_FIX_STARTED
REVIEW_FIX_VALIDATION_PASSED
REVIEW_FIX_VALIDATION_FAILED
REVIEW_ESCALATED
READY_FOR_HUMAN
```

---

## Tests

最低限:

- non-CodeRabbit actor rejected
- duplicate ignored
- processed review ignored
- outdated review behavior
- allowed path review
- forbidden path review
- valid structured classification
- invalid classifier JSON
- ACTIONABLE high confidence
- ACTIONABLE low confidence
- NON_ACTIONABLE
- OUT_OF_SCOPE
- CONFLICTS_WITH_SPEC
- UNCERTAIN
- review attempt increment
- review attempt limit
- review fix scope violation

LLM APIはmock可能にする。

---

## Definition of Done

- CodeRabbitを非同期eventとして処理できる
- event payloadだけをレビュー全体と見なさない
- deterministic pre-filterがある
- semantic classifierがStructured Outputを返す
- classifier結果をschema validationする
- deterministic policyを必ず通す
- CodexへACTIONABLEな指摘だけを渡す
- review fix後もScope Checkする
- Review Limitを超えない
- Spec conflict / low confidenceをEscalateする
- tests PASS

---

## Final System Outcome

このPhase完了後:

```text
Task Spec
  ↓
GitHub Actions
  ↓
Codex Implementation
  ↓
Scope / Validation / Repair
  ↓
PR
  ↓
CodeRabbit
  ↓
Review Policy
  ↓
Codex Review Fix
  ↓
Human Merge
```

が成立する。

---

## Stop Condition

このPhase完了後は、
システム全体のE2E結果とManual Setup Requiredを報告して停止する。
