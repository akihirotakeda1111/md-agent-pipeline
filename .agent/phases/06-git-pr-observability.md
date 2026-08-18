# Phase 6 — Git, Pull Request, Restart / GitHub Reconciliation & Observability

## Objective

Phase 5のExecution Workflowへ、
安全なCommit / Push / PR作成、Restart / GitHub Reconciliation、
GitHub上の可観測性を追加する。

このPhase終了時点で、
CodeRabbitなしでも実用可能なMD駆動自律開発MVPを完成させる。

---

## Depends On

Phase 5 — GitHub Actions Execution

---

## Git Policy

原則:

```text
1 Task Spec
=
1 Work Unit
=
1 Feature Branch
=
1 Pull Request
```

Codex自身にはGit操作を担当させない。

---

## Commit Policy

全TaskのValidationおよびFinal VerificationがPASSし、
deliver側で最終的なContext / Scope / Manifest検証を通過した後にのみ、
Orchestratorがcommitを作る。

例:

```text
feat(worker): implement job lease repository
feat(worker): add SQS heartbeat
infra(cloudwatch): add backlog alarm
```

禁止:

- force push
- history rewrite
- Final Verification前commit
- scope violationを含むcommit
- 未検証patchのcommit

---

## Write Authority Isolation

Executionを以下に分離する。

```text
Execute Job
  contents: read
  Codex API credential
  ↓
report.json + changes.patch + patch_sha256
  ↓
Deliver Job
  contents: write
  pull-requests: write
  issues: write
  Codex API credentialなし
```

Codex API credentialとGitHub write credentialを
同一processへ同時に露出しない。

CodexはGit writeを行わず、
Commit / Push / Pull Request作成はDeliver側Orchestratorのみが行う。

公式Codex GitHub Actionはsandbox / 実行環境のbootstrap用途に限定し、
Coding EngineはOrchestrator配下のCodex CLIのままとする。

---

## Final Verification

全Task成功後、delivery前にTask SpecのFinal Verificationを実行する。

Execute側でPASSした後も、
新規deliveryではDeliver側でpatch適用後の実treeに対して
Final Verificationを再実行する。

同一work unitと確認済みの既存PRを再利用する場合は、
patch再適用もFinal Verification再実行もしない。

FAILした場合:

- repairableならExecute側のbounded repair
- environment failureならFAILED
- escalation requiredならESCALATED
- 新規deliveryのDeliver側再検証失敗ならCommit / Push / PRを行わず停止

新規deliveryではPASSするまでPRをREADY扱いにしない。
再利用する既存PRは、過去のdeliveryでVerification済みとしてREADYのまま扱う。

---

## Delivery Verification

DeliverはExecuteのreportをそのまま信頼せず、
Git write前に実体を再検証する。

原則的な順序:

```text
report ↔ Task Spec context validation
→ patch_sha256 validation
→ existing PR / branch reconciliation
```

同一work unitの既存PRがあればここで再利用して終了する。
その場合はpatch再適用、Scope再検証、Final Verification再実行をしない。

新規deliveryのみ、続きを必須とする。

```text
→ HEAD == report.base_sha
→ clean working tree / index
→ apply patch
→ collect actual changes
→ Scope Enforcement
→ manifest validation
→ Final Verification
→ Commit
→ Push
→ Pull Request
```

Contextとして最低限以下を照合する。

```text
spec_id
spec_path
target branch
base SHA
```

`report.patch_sha256` はartifactとして受け渡された
`changes.patch` の実バイト列と一致しなければならない。

patch適用後はreportの自己申告ではなく、
Gitの実差分をSource of TruthとしてScopeを再検証する。

`.agent/state/**` をScope検査から除外しない。
Codex / patchによる `.agent/state/**` の変更はScope Violationとする。

commit対象はreport上のchanged filesではなく、
再取得した実差分のうちScopeを通過したpathとする。

実差分のpath集合と `report.changed_files` が一致しない場合は
`PATCH_MANIFEST_MISMATCH` としてEscalateする。

---

## Pull Request

全Task + Final Verification + Deliver Verification成功後のみ作成する。

PR本文に最低限:

```text
Task Spec
Objective
Completed Tasks
Changed Files
Validation Results
Final Verification
Repair Attempts
Known Limitations
Human Review Points
Escalation History
```

を含める。

---

## Labels

Phase 6で実際に適用するのは以下のみ。

```text
agent:ready
agent:escalated
agent:failed
```

未作成ならDeliver jobが作成する。
必要permissions: `issues: write`（または `pull-requests: write`）。

`agent:running` は作成・適用しない。
executeへGitHub write権限を足さない。

`agent:review` はPhase 7の責務であり、このPhaseでは適用しない。

---

## FAILED vs ESCALATED

### FAILED

再実行で回復する可能性がある。

例:

- GitHub API failure
- package registry outage
- temporary network error
- internal workflow error

### ESCALATED

人間判断が必要。

例:

- IAM追加
- destructive migration
- spec contradiction
- architecture decision
- repair limit
- unsafe Git / PR reconciliation
- report / patch / base SHA inconsistency

---

## Human Notification

ESCALATED時:

PRがある:
- PR comment
- `agent:escalated`

PRがない:
- Issue または Workflow Summary等で人間が認識可能にする

最低限:

```text
Task ID
Current Task
Reason
Last Validation
Repair Attempts
Required Human Action
```

mention先はconfigurable。
未設定時に勝手なユーザー名を生成しない。

---

## Restart / GitHub Reconciliation

実行中のExecution State JSONはGitHub-hosted runner上でephemeralとする。

GitHub Actions再実行時はwork unitを途中からResumeせず、
Task Specから最初から再実行する。

過去のCodex会話やExecution StateをSource of Truthにしない。

Durableな照合対象はGitHub上に既に存在する外部状態とする。

```text
Git branch
Git history / commits
Pull Request
GitHub labels/comments
Task Spec
```

`.agent/state/*.json` はephemeral runtime metadataである。

- GitHub Actions再実行のResumeソースにはしない
- GitHub Actionsではwork unitを最初から再実行する
- ローカル実行では同一workspaceの実行中制御に利用してよい
- MVPではGitへcommitしない

deliver時:

- 同一work unitと確認済みの既存open PRは再利用する
- reuse時はpatch再適用やFinal Verification再実行をしない
- reuse時は `PR_CREATED` を emit しない。outcome は既存PR再利用でも `PR_CREATED` のままにする
- `spec_id`、target branch、base branch、PR work-unit markerを照合する
- 同じbranchのPRが存在するだけでは再利用しない
- 同一work unitと確認できなければ自動適用 / rebaseせずESCALATED
- rebase / force push / history rewriteはしない
- 新規deliveryだけDeliver側Verificationを必須とする

Codex / patchが `.agent/state/**` を変更した場合はScope Violationとする。

---

## GitHub Reconciliation

Execution StateをReconciliationのSource of Truthにしない。
durable sourceはGit / branch / commit / Pull Requestとする。

例:

```text
open PR exists
AND marker/head/base identify the same work unit
→ reuse it; do not create a second PR
→ do not re-apply the patch
→ do not re-run Final Verification
```

```text
open PR exists on the target branch
BUT work unit identity is missing or different
→ ESCALATED; do not apply or rebase
```

新規deliveryでは、Execute時に記録した `report.base_sha` を
Git writeのparentとして固定する。

```text
HEAD == report.base_sha
→ delivery継続
```

```text
HEAD != report.base_sha
→ ESCALATED
→ rebase / mergeしない
```

照合対象:

- spec_id
- target branch / PR head.ref
- base branch / PR base.ref
- PR work-unit marker
- report.base_sha
- git history
- patch digest

安全に照合できない場合はESCALATED。
自動rebaseしない。

---

## Observability

Structured events:

```text
SPEC_DISCOVERED
SPEC_VALIDATED
STATE_INITIALIZED
TASK_STARTED
CODEX_STARTED
CODEX_COMPLETED
SCOPE_CHECK_STARTED
SCOPE_CHECK_PASSED
SCOPE_VIOLATION
VALIDATION_STARTED
VALIDATION_PASSED
VALIDATION_FAILED
REPAIR_STARTED
TASK_COMPLETED
FINAL_VALIDATION_STARTED
FINAL_VALIDATION_PASSED
DELIVERY_VALIDATION_STARTED
DELIVERY_VALIDATION_PASSED
PR_CREATED
ESCALATED
FAILED
WORKFLOW_COMPLETED
```

可能ならJSON Lines。

---

## GitHub Actions Summary

最低限:

```text
Task Spec
Task ID
State
Current Task
Completed Tasks
Changed Files
Validation Results
Repair Attempts
PR URL
Failure Reason
Escalation Reason
```

---

## Tests

最低限:

- commit only after Final Verification and Deliver Verification
- no commit on scope violation
- deliver detects out-of-scope paths introduced by patch
- no PR before final verification
- PR body generation
- report/spec context mismatch blocks delivery
- base SHA mismatch blocks delivery without rebase
- patch digest mismatch blocks delivery
- patch manifest mismatch blocks delivery
- restart from missing ephemeral state starts work unit from the beginning
- reuse existing pull request only for the same work unit
- failed vs escalated policy
- escalation notification payload
- summary rendering

---

## Definition of Done

実GitHub設定が必要な部分を除き、以下が成立する。

```text
MD
→ Actions
→ Spec Validation
→ Codex
→ Scope Check
→ Validation
→ Repair
→ Final Verification
→ Delivery Verification
→ Commit
→ Push
→ PR
```

さらに:

- GitHub Actions再実行時はwork unitを最初から再実行する
- 同一work unitに対応する既存PRのみdeliverで再利用する
- report / patch / Git contextをGit write前に照合できる
- patch適用後の実差分をScope再検証できる
- Git / PR不整合を検出可能
- unsafeな不整合では自動rebaseせずEscalateする
- human-readable summaryがある
- write authorityがCodexから分離される
- tests PASS

---

## Manual Setup Required

このPhase終了時に、最低限以下を文書化する。

- GitHub Secrets
- Actions permissions
- branch protection
- labels
- Codex authentication
- optional notification target

---

## Stop Condition

DoD後、Phase 7へ進まず停止する。
