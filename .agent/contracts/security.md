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
- PR create PAT (`AGENT_PR_PAT`)
- production AWS credentials
- production DB credentials
- deployment credentials
- review-classifier API key
- unrelated repository secrets

GitHub write authorityはOrchestrator側だけに保持する。

### PR Create Credential Isolation

CodeRabbit auto review は `github-actions[bot]` が author の PR では発火しない。
そのため Pull Request 作成だけを fine-grained PAT（Actions Secret `AGENT_PR_PAT`）で行う。

原則:

```text
GitHub Actions Secret AGENT_PR_PAT
        ↓
deliver job / deliver.py
        │
        └─ GitHubClient.create_pull() のみ
```

次には渡してはいけない:

- Codex subprocess
- Review Classifier
- git commit / git push
- 既存 PR 検索・reconciliation
- label / comment / tracking
- execute job / review job / parse-spec
- その他の GitHub API 操作

`AGENT_PR_PAT` が無いときの create_pull は fail closed（`MISSING_AGENT_PR_PAT`）。
`GITHUB_TOKEN` への fallback はしない。commit / push は `GITHUB_TOKEN` のみ。

PAT 所有者を PR author にすることで CodeRabbit auto review が発火する。
PAT による `pull_request` event は Actions workflow を起こし得るが、
`agent-execute.yml` は `pull_request` を購読せず、push も `GITHUB_TOKEN` のため再帰しない。
`agent-review.yml` は CodeRabbit の terminal check/status だけを購読する。

### Codex API Credential Isolation

Codex API credentialは、Codex実行に必要な最小範囲にのみ公開する。

GitHub ActionsではCodex API credentialをRepository Secret等のSecret Storeから取得し、
Codexを実行するOrchestrator step以外のstepへ渡してはいけない。

Orchestrator processがCodex API credentialを受け取った場合、
そのcredentialを子processへ暗黙継承させてはいけない。

原則:

```text
GitHub Actions Secret
        ↓
Orchestrator
        │
        ├─ Codex subprocess
        │    └─ Codex API credentialあり
        │
        ├─ Validation subprocess
        │    └─ Codex API credentialなし
        │
        ├─ Git subprocess
        │    └─ Codex API credentialなし
        │
        └─ Other subprocess
             └─ Codex API credentialなし
```

Codex API credentialは、Codex subprocessのenvironmentへ明示的に注入する。

Validation、Git、repository-controlled command、その他のsubprocessは、
明示的に構築されたsanitized environmentで実行し、
Codex API credentialを含むSecretを暗黙継承してはいけない。

OrchestratorがGitHub Actionsのstep environmentからCodex API credentialを受け取る場合は、
必要な値を取得した後、通常のprocess environmentから除去し、
Codex起動時にのみ明示的に再注入する。

以下を禁止する:

- Codex API credentialをworkflow全体のenvironmentへ設定する
- Codex API credentialをjob全体のenvironmentへ設定する
- Validation commandへCodex API credentialを渡す
- Git commandへCodex API credentialを渡す
- repository-controlled subprocessへCodex API credentialを渡す
- Codex API credentialをlogへ出力する
- Codex API credentialをartifactへ保存する
- Codex API credentialをrepositoryへ保存する
- 親processのenvironmentを無条件に子processへ継承する

新しいsubprocessを追加する場合も、
Secretを必要とすることが明示されていない限りsanitized environmentを使用する。

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
