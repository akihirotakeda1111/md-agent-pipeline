# Phase 3 — Codex CLI Runner

## Objective

OpenAI公式Codex CLIを、
Orchestrator配下の制限されたImplementation Engineとして起動できるようにする。

CodexにGit / State / PR / RetryのControl Plane権限を与えない。

---

## Depends On

Phase 2 — Task Spec & Execution State

---

## External Verification Required

実装時点のOpenAI公式Codex documentationを確認すること。

確認対象:

- official installation method
- current CLI version
- non-interactive invocation
- sandbox option
- CI authentication
- GitHub Actions向け公式推奨
- deprecated flags

推測でCLI option/packageを作らない。

---

## Mandatory Backend

Coding backendはOpenAI公式Codex CLI。

以下は禁止:

- Aiderへ置換
- generic agent frameworkへ置換
- Codex SDKへ無断置換
- 第三者の同名 `codex-cli` package
- 存在確認していないinstall command

GitHub Actions上で公式 `openai/codex-action` をtransportとして使う場合でも、
coding engineが公式Codex CLIである責務境界を維持すること。

---

## Runner Contract

Runnerは最低限以下をinputに取れること。

```text
Task Spec
Current Task
Allowed Paths
Forbidden Actions
Architecture Invariants
Acceptance Criteria
Repository working directory
```

Outputとして最低限:

```text
process exit code
stdout/stderr or safe execution log
Codex final response
run metadata
```

をOrchestratorへ返す。

---

## Codex Instruction

以下の意味を持つpromptを実装する。

```text
You are the implementation engine inside a deterministic autonomous development system.

The Task Specification is authoritative.

Your responsibility is limited to inspecting relevant repository code
and making the smallest implementation change necessary to satisfy
the current task.

Priority:
1. Safety constraints
2. Forbidden Actions
3. Architecture Invariants
4. Allowed Scope
5. Acceptance Criteria
6. Existing repository conventions

You MUST NOT:
- modify the Task Specification
- modify Execution State
- modify CI workflows
- modify autonomous-agent infrastructure
- modify files outside allowed paths
- remove or weaken tests
- disable lint rules
- modify secrets
- perform destructive infrastructure operations
- force push
- rewrite git history
- create or merge Pull Requests
- manipulate the orchestration system

Validation, retry control, state transitions, git operations,
review policy and escalation are controlled externally.

If the task requires violating the contract, report the reason
instead of bypassing the constraint.
```

---

## Sandbox

Non-interactive Codex executionには、
実装時点の公式ドキュメントで推奨されるworkspace write sandboxを利用する。

sandbox外へのwriteを不要に許可しない。

---

## Authentication

CI用認証は公式のprogrammatic authentication方式を利用する。

Requirements:

- Secretをrepositoryへcommitしない
- Secretをlogへ表示しない
- Codex実行step以外へ不必要に露出させない
- GitHub write tokenとCodex credentialを分離する

Secret名はproject内で一貫させる。

---

## Version Pinning

Codex CLI versionを明示的に管理する。

`latest` 暗黙追従を避ける。

version upgradeは意図的なPlatform変更として扱う。

---

## Context Strategy

repository全文をpromptへ貼らない。

Codex自身に必要なfilesを段階的にinspectさせる。

初期promptに含めるのは主に:

- Current Task
- relevant contract
- allowed paths
- acceptance criteria
- repository location

---

## No Git Control

Codex runnerに以下を担当させない。

- branch creation
- commit
- push
- PR
- labels
- comments
- state update
- retry count

---

## Tests

外部APIを常時呼ばなくてもtestできるようにする。

最低限:

- command construction
- prompt construction
- environment allowlist
- secret exclusion
- exit code propagation
- timeout/error propagation
- mock Codex success
- mock Codex failure

---

## Definition of Done

- 公式Codex CLIをnon-interactiveに起動するrunnerがある
- versionが明示管理される
- promptをCurrent Taskから生成できる
- Codex subprocess environmentが制限される
- GitHub write credentialをCodexへ渡さない
- success/failureをOrchestratorへ返せる
- unit testsがPASSする

---

## Not In This Phase

- Validation commandsの実行
- Repair loop
- GitHub Actions
- Commit / Push
- PR
- CodeRabbit

---

## Stop Condition

DoD後、Phase 4へ進まず停止する。
