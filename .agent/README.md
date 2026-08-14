# `.agent` Implementation Guide

## Cursorへの最初の指示

まず以下だけを渡してください。

```text
.agent/bootstrap.md を読み、その指示に従ってください。
Global Contractsを確認したうえで、未完了の最初のPhaseを1つだけ実装してください。
そのPhaseのDefinition of Doneを満たしても、次Phaseへは進まず停止してください。
```

Phaseを明示する場合:

```text
.agent/bootstrap.md と .agent/phases/03-codex-runner.md を読み、
Phase 3だけを実装してください。
Global Contractsを優先し、DoD完了後は停止してください。
```

---

## Structure

```text
.agent/
├── README.md
├── bootstrap.md
├── implementation-plan.md
├── contracts/
│   ├── architecture.md
│   ├── security.md
│   └── invariants.md
└── phases/
    ├── 01-foundation.md
    ├── 02-spec-state.md
    ├── 03-codex-runner.md
    ├── 04-scope-validation-repair.md
    ├── 05-github-actions.md
    ├── 06-git-pr-observability.md
    ├── 07-coderabbit-review.md
    └── TEMPLATE.md
```

---

## Why One Phase Per Session

一度に全Phaseを実装させると、Agentが以下を同時に判断する必要がある。

- Spec schema
- State machine
- Codex CLI
- Git safety
- GitHub Actions
- PR
- CodeRabbit
- LLM classifier

Phaseを分けることで、
一度の意思決定空間と変更範囲を限定する。

---

## Completion Strategy

推奨:

```text
Phase 1
→ human review / commit
→ Phase 2
→ human review / commit
→ ...
```

各Phaseのcommitを明確に分けると、
問題発生時にrollbackしやすい。
