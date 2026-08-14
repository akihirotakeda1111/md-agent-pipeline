# Global Invariants

以下は全Phaseを通じて維持する。

## I-01 — Spec Ownership

Task SpecはHuman-owned。
Runtime Codexは変更しない。

## I-02 — Runtime State Separation

Runtime StateはTask Spec本文へ書き込まない。
Machine-owned JSONとして分離する。

## I-03 — Codex Is Not the Control Plane

Codex CLIは実装・修正のみ。
State / Retry / Git / PR / Review PolicyはOrchestratorが制御する。

## I-04 — Validation Is External

Validation commandの実行結果はOrchestratorが取得し、
exit codeを状態遷移の根拠にする。

## I-05 — Scope Is Mechanically Enforced

Codexへの自然言語指示だけでScopeを制限しない。
Git差分を機械検証する。

## I-06 — One Spec, One Work Unit

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

## I-07 — No Automatic Destructive Infrastructure

Terraform apply/destroy等は自律実行しない。

## I-08 — Retry Is Bounded

Repair / Review Fixには上限を持つ。
上限超過後にLLMを呼び続けない。

## I-09 — Environment Failure Is Not a Coding Failure

network、registry、credential、GitHub outage等を
Codexのコード修正で解決しようとしない。

## I-10 — Review LLM Has No Execution Authority

Review classifierの結果は必ず通常コードのPolicy Engineを通す。

## I-11 — Resume Does Not Depend on Conversation History

再開にCursor/Codexの過去会話を必要としない。

## I-12 — External Syntax Is Verified

Codex CLI / GitHub Actions / OpenAI API / CodeRabbitの
現在仕様を推測しない。
必要時は公式ドキュメントを確認する。
