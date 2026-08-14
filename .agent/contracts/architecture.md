# Architecture Contract

## Core Model

このシステムは3種類の責務を分離する。

### Codex CLI

```text
Think
Inspect
Implement
Repair
```

Codex CLIは非決定的なImplementation Engineとして扱う。

### Semantic Classifier

```text
Classify natural-language meaning only
```

自然言語レビューの意味分類だけを担当する。
Control Planeにはしない。

### Deterministic Orchestrator

```text
Parse
Validate
Restrict
Select
Schedule
Lock
State
Retry
Git
PR
Policy
Escalate
```

Workflowの状態変更・権限操作はOrchestratorが担当する。

---

## State Transition Principle

LLMの自己申告だけを根拠に状態遷移してはいけない。

優先して利用する根拠:

- file system
- parsed schema
- git diff
- command exit code
- Git commit history
- GitHub API state
- Execution State JSON

---

## Ownership

### Human-owned

```text
specs/tasks/**
```

Runtime Codexは編集不可。

### Orchestrator-owned

```text
.agent/state/**
```

Codexは編集不可。

### Platform-owned

```text
.github/workflows/**
agent/**
```

Runtime Task実行中のCodexは編集不可。

### Agent-editable

Task Specの `allowed_paths` に明示されたApplication Sourceのみ。

---

## Runtime Data Flow

```text
Task Spec
  ↓
Spec Parser
  ↓
Schema Validator
  ↓
State Machine
  ↓
Task Selector
  ↓
Codex Runner
  ↓
Working Tree Diff
  ↓
Scope Policy
  ↓
Validation Runner
  ↓
Retry Policy
  ↓
Git / PR
```

---

## Design Constraint

初期versionで以下を導入しない。

- Kubernetes
- external workflow engine
- message broker
- agent state database
- distributed scheduler
- multi-agent framework
- generic agent abstraction framework

GitHubをControl Planeとして利用する。
