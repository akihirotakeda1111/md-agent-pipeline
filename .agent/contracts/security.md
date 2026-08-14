# Security Contract

## Primary Rule

**Prompt is not a security boundary.**

LLMへの禁止指示だけで安全性を担保しない。
可能な制約はfilesystem、Git、GitHub permissions、sandbox、credential isolation、
schema、policy codeで強制する。

---

## Codex Credential Boundary

Codex subprocessに不要なcredentialを渡してはいけない。

原則としてCodexへ渡さないもの:

- GitHub write token
- production AWS credentials
- production DB credentials
- deployment credentials
- review-classifier API key
- unrelated repository secrets

GitHub write authorityはOrchestrator側だけに保持する。

---

## Forbidden Runtime Actions

Runtime Codexに以下を許可しない。

- force push
- Git history rewrite
- PR merge
- Task Spec変更
- Execution State変更
- GitHub Workflow変更
- Orchestrator infrastructure変更
- secret変更
- destructive migration
- `terraform apply`
- `terraform destroy`
- `terraform state rm`
- `terraform state mv`
- IAM policy追加

Taskがこれらを必要とする場合はEscalationとする。

---

## Diff Enforcement

Codexの実行後は、必ず実際のGit差分から変更対象を検査する。

`allowed_paths` 外の変更が1件でも存在すれば:

```text
SCOPE_VIOLATION
```

としてCommit / Pushを禁止する。

---

## Fail Closed

以下が曖昧な場合は「許可」と推定しない。

- pathがallowedか
- reviewがscope内か
- destructive changeか
- credentialが必要か
- state reconciliationが安全か

安全に判断できない場合はEscalateする。

---

## Untrusted Events

PRやcomment等の外部入力を扱うWorkflowでは、
fork由来コードやuntrusted payloadとSecretsを同じtrusted contextで実行しない。

GitHub Eventの種類とpermissionsを確認し、
Secret exposureにつながる構成を避ける。
