# Phase 6 — Git, Pull Request, Resume & Observability

## Objective

Phase 5のExecution Workflowへ、
安全なCommit / Push / PR作成、Resume、State Reconciliation、
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

TaskのValidation PASS後、
Orchestratorが意味のあるcommitを作る。

例:

```text
feat(worker): implement job lease repository
feat(worker): add SQS heartbeat
infra(cloudwatch): add backlog alarm
```

禁止:

- force push
- history rewrite
- validation前commit
- scope violationを含むcommit

---

## Write Authority Isolation

可能ならExecutionを以下に分離する。

```text
Codex Job
  contents: read
  ↓
patch/diff artifact
  ↓
Write Job
  contents: write
  pull-requests: write
```

Codex API credentialとGitHub write credentialを
同一processへ同時に露出しない構造を優先する。

公式Codex GitHub Actionのcurrent security patternも確認すること。

---

## Final Verification

全Task成功後、PR作成前にTask SpecのFinal Verificationを実行する。

FAILした場合:

- repairableならbounded repair
- environment failureならFAILED
- escalation requiredならESCALATED

PASSするまでPRをREADY扱いにしない。

---

## Pull Request

全Task + Final Verification成功後のみ作成する。

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

最低限以下を扱えるようにする。

```text
agent:running
agent:review
agent:ready
agent:escalated
agent:failed
```

Labelが存在しない場合のpolicyを決める。
自動作成するなら必要permissionsを明示する。

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
- state inconsistency

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

## Resume

過去のCodex会話をresumeのSource of Truthにしない。

以下だけから再開可能にする。

```text
Execution State JSON
Git branch
Git history
Pull Request
GitHub labels/comments
Task Spec
```

---

## State Reconciliation

Execution Stateを絶対視しない。

例:

```text
State = TASK_COMPLETED
but corresponding commit missing
```

などを検出する。

照合対象:

- state
- branch
- git history
- PR state

安全に修復できない場合はESCALATED。

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

- commit only after validation
- no commit on scope violation
- no PR before final verification
- PR body generation
- state/commit mismatch
- state/branch mismatch
- resume from valid state
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
→ Commit
→ Push
→ PR
```

さらに:

- workflow再実行からresume可能
- State/Git不整合を検出可能
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
