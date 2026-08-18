# Phase 5 — GitHub Actions Execution

## Objective

Task Specのbase branchへの投入をGitHub Actionsで検知し、
Task ID単位で安全にOrchestratorを起動できるようにする。

このPhaseではPR作成を完成させない。
まずExecution Workflowの安全性・排他・履歴取得を完成させる。

---

## Depends On

Phase 4 — Scope Enforcement, Validation & Repair

---

## Workflow

作成対象:

```text
.github/workflows/agent-execute.yml
```

概念フロー:

```text
push / workflow_dispatch
  ↓
parse-spec job
  ↓
task_id output
  ↓
execute job
  ↓
job-level concurrency
  ↓
orchestrator
```

---

## Trigger

原則:

```text
base branch
+
specs/tasks/**/*.md
```

へのTask投入をtriggerとする。

feature branch上のAgent自身の通常Pushで
Task Intake workflowが再帰起動しない設計にする。

`[skip ci]` を主たるloop preventionにしない。

---

## Parse Spec Job

Workflow開始時に先行jobでSpecをparseし、
最低限以下をoutputする。

```text
task_id
spec_path
base_branch
target_branch
valid
```

Invalid Specの場合はCodex execution jobを開始しない。

---

## Job-Level Concurrency

Task IDを先行job outputとして取得し、
後続execution jobのjob-level concurrencyに利用する。

概念:

```yaml
jobs:
  parse-spec:
    outputs:
      task_id: ...

  execute:
    needs: [parse-spec]
    concurrency:
      group: autonomous-agent-${{ needs.parse-spec.outputs.task_id }}
      cancel-in-progress: false
```

実装時点のGitHub Actions仕様を公式docsで確認すること。

Branch存在確認だけをmutexとして扱わない。

---

## Multi-Layer Locking

最低限:

### Layer 1
GitHub Actions task-level concurrency

### Layer 2
Execution State guard（ephemeral execution control。GHA再実行のResumeソースではない）

### Layer 3
Git / branch / PR reconciliation（Phase 6のdeliver側 durable GitHub reconciliation）

を用意する。

---

## Git Checkout / History

Scope Check、merge-base、deliver側 GitHub reconciliation に必要なcommit/refを
ローカルに確実に取得する。

MVPではfull history checkoutでもよい。

ただし重要なのは:

```text
comparisonに必要なcommit objectが存在すること
```

であり、特定のfetch-depth値そのものを目的化しない。

---

## Checkout Credential Safety

Codexを実行するjobでは、
可能ならGitHub write credentialをpersistしない。

read-only Codex executionと、
writeを必要とする後段を分離できる設計を優先する。

実装時点の公式`actions/checkout`のsecurity behaviorを確認する。

---

## Permissions

Workflow/jobごとに最小権限を明示する。

Codex execution側では原則:

```text
contents: read
```

から開始し、必要性が証明された権限だけ追加する。

Phase 6でGit write / PR writeを別jobへ分離できる設計にしておく。

---

## Secret Scope

Codex用SecretはCodex実行に必要なstep/jobだけへ渡す。

他のsetup/test stepへ不必要にexportしない。

fork/untrusted eventからSecret-bearing workflowを起動しない。

---

## Manual Trigger

`workflow_dispatch` から特定Specを指定して
安全に再実行できるinputを用意する。

---

## Loop Prevention

以下を組み合わせる。

- base branch/path trigger
- Spec/State guard
- Task concurrency
- reconciliation

`[skip ci]` だけへ依存しない。

---

## Tests / Validation

最低限:

- workflow YAML parse
- invalid spec skips execution
- parse job exposes task_id
- execute uses task_id concurrency
- required history is available
- feature branch push does not become new task intake
- permissions are explicit
- Codex secret is not globally exposed

可能ならGitHub Actionsのstatic validation toolを利用する。

---

## Definition of Done

- Task Spec pushからExecution Workflowが起動する構造がある
- Spec parseをCodex前に行う
- task_id単位でjob concurrencyを設定できる
- executionに必要なGit historyを取得できる
- secret / permissionsがjob単位で制限される
- recursive triggerを多層で防ぐ
- workflow validationがPASSする

---

## Stop Condition

DoD後、Phase 6へ進まず停止する。
