# Phase 5–6 Real GitHub E2E Smoke Test

## Purpose

Production `.github/workflows/agent-execute.yml`をそのまま使用し、GitHub-hosted runner、Real Codex CLI、Production execute/deliverを経由して、実GitHub上にCommit、Push、Pull Requestが作られることを1シナリオで確認します。

既存テストを置き換えません。

```text
Phase 5 Integration
  GitHub Actions / runner / Real Codex execution platform

Phase 6 deterministic Integration
  Production Orchestrator / Real Git / Fake GitHub / detailed failure contracts

This E2E Smoke
  Production Workflow / Real Codex / Real GitHub Commit, Branch and Pull Request
```

Phase 7、CodeRabbit、review loopは対象外です。

## Directory structure

```text
github-pr-e2e/
|-- README.md
|-- run.py
|-- self_test.py
|-- requirements.txt
|-- fixtures/
|   `-- phase6-e2e-task.md
|-- harness/
|   |-- assertions.py
|   |-- git.py
|   |-- github.py
|   |-- models.py
|   |-- process.py
|   |-- source_contracts.py
|   `-- workflow.py
`-- reports/
```

Production repositoryでは、既存Phase 5/6 suitesと分離して配置します。

```text
agent/integration-tests/
|-- github-actions/
|-- git-pr-observability/
`-- github-pr-e2e/
```

## No shadow implementation

このsuiteは次を作成・使用しません。

- shadow workflowまたはProduction workflowのコピー
- Fake Codex
- Fake GitHub API
- shadow Orchestrator
- E2E専用Production trigger
- E2E専用Production policy

Harnessが担当するのは、isolated source branch/Task Specの作成、既存workflowの起動、GitHub APIによる観測、assertion、cleanupだけです。

## Source of Truth preflight

cloneしたProduction refから次を読み、SHA-256をresult reportへ保存します。

- `.agent/bootstrap.md`
- `.agent/phases/05-github-actions.md`
- `.agent/phases/06-git-pr-observability.md`
- `.agent/contracts/**/*.md`または`.agent/**/*contract*.md`
- `.github/workflows/agent-execute.yml`
- `README.md`

canonical sourceが欠落している場合、コピーを生成せず`PRODUCTION_GAP`として停止します。Workflow自身のexecute/deliver entry pointはProduction YAMLが参照する実装をそのまま使用します。

## Scenario

実行ごとにunique suffixを生成し、次を専用化します。

```text
temporary base / source branch: e2e/phase6-<unique-id>
Task Spec:     specs/tasks/_e2e-phase6-<unique-id>.md
Task ID:       phase6-e2e-<unique-id>
target branch: agent/phase6-e2e-<unique-id>
generated file: app/e2e-phase6-<unique-id>.txt
```

Task Spec の `base_branch` は temporary source branch 自身です。現行 Production intake（`ref == spec.base_branch`）を変えずに execute を通すためです。PR base もこの temporary branch です。repository default branch へは commit しません。

Taskは専用text fileを1つ作成するだけです。Scopeはその1 pathだけを許可し、`.agent/state/**`、`.github/**`、`agent/**`、`specs/**`をCodex変更対象として禁止します。Terraform、deployment、migration、AWS/DB操作はありません。

## Run 1 — Real PR creation

1. repository default branchを一時directoryへclone
2. Production contracts/workflowを確認
3. temporary base branchに専用Task Specだけをcommit
4. 既存 Production `push` trigger で workflow を起動
5. workflow ID/name/path + ref + HEAD SHA + eventでrunを検索
6. 一致0件または複数件ならFAIL
7. workflow、execute job、deliver jobのsuccessを確認
8. target branch、delivery commit、changed fileを実GitHub APIで確認
9. open PRが正確に1件であることを確認
10. head/base、work-unit marker、PR sections、`agent:ready`を確認

Delivery head commitとPR差分はgenerated fileだけを含む必要があります。Task Specはtemporary base上にあるためPR差分には出ません。

## Run 2 — Restart and reconciliation

Run 1のGitHub Actions runをrerunします。同じworkflow ID、source ref、HEAD SHA、event、work unitを維持し、`run_attempt`だけを増やします。

```text
same Task Spec / same source SHA
-> Production execute starts again without durable ephemeral state
-> Production deliver finds existing open PR
-> spec_id + head + base + work-unit marker reconciliation
-> same PR reused
```

次を確認します。

- rerun conclusion、execute、deliverがsuccess
- run identity検索結果が引き続き1件
- open PR数が1件
- PR number/URL/head/base/head SHAがRun 1と同じ
- feature branch SHAが変化していない
- duplicate PR/commitが作られていない

Run 2 logsからstructured JSON eventを抽出できる場合、`DELIVERY_VALIDATION_STARTED`、`DELIVERY_VALIDATION_PASSED`、`PR_CREATED`がないことも確認します。イベントがlogsへ公開されていない場合は`not_observable`としてreportへ記録し、E2EのためにProduction eventを追加しません。

## Existing trigger selection

defaultは`--trigger push`です。temporary base branchへ Task Spec を push し、現行 intake をそのまま通します。E2E専用triggerは使いません。

`--trigger auto` は既存push filtersがsource branchとTask Spec pathに一致すればpush、一致しなければ既存`workflow_dispatch`、どちらも使えなければ`E2E_SAFE_TRIGGER_UNAVAILABLE`です。本番契約を変えないため、このsuiteの受け入れ経路はpushです。

workflow_dispatchではProduction YAMLに定義済みのinputsだけを使用します。`spec_path`/`task_spec`や`task_id`相当は自動設定します。その他のrequired inputは明示します。

```powershell
--dispatch-input mode=normal
```

未定義input、shadow trigger、default branchへの直接commitは使用しません。

## Authentication and Manual Setup

Harness credentialとProduction workflow credentialは別です。

Harnessを実行する`gh` credentialには、対象repositoryについて次が必要です。

- source branchのpush/delete
- Actions run read/rerun
- Pull Request read/close
- feature branch cleanup

Production workflowは既存`GITHUB_TOKEN` permissionsと既存`CODEX_API_KEY` secretを使用します。Harnessは`CODEX_API_KEY`を読み取り・受け渡し・保存しません。

repository側では既存Phase 5/6 Manual Setupが完了している必要があります。

- Production secret `CODEX_API_KEY`
- GitHub ActionsからのPull Request作成許可
- Production execute/deliver job permissions
- Production workflowがactive

E2EのためにProduction permissionsを追加しないでください。

## Installation and local self-test

```powershell
python -m pip install -r agent/integration-tests/github-pr-e2e/requirements.txt
python agent/integration-tests/github-pr-e2e/self_test.py
```

`self_test.py`はnetworkやGitHub resourceを使用せず、fixture rendering、naming、trigger selection、dispatch interface、assertions、report schemaを検証します。

## Real GitHub execution

repository rootから実行します。

```powershell
python agent/integration-tests/github-pr-e2e/run.py --repo OWNER/REPO
```

調査用にresourceを残す場合:

```powershell
python agent/integration-tests/github-pr-e2e/run.py --repo OWNER/REPO --keep-resources
```

再現可能なsuffixを指定する場合:

```powershell
python agent/integration-tests/github-pr-e2e/run.py --repo OWNER/REPO --unique-id 20260818-manual01
```

## Cleanup

全assertionとevidence取得後、defaultでは次を実行します。

1. E2E PRをclose
2. target feature branchをdelete
3. source branchをdelete

assertion前にresourceを削除しません。失敗時もresult reportを書いたうえでcleanupします。調査のため残す場合は`--keep-resources`を指定してください。

Cleanup failureはE2E assertion結果と分離します。

```text
PASS
PASS_CLEANUP_FAILED
FAIL
FAIL_CLEANUP_FAILED
```

`PASS_CLEANUP_FAILED`は非ゼロ終了です。

## Result report

`reports/github-pr-e2e-<unique-id>.json`を生成します。Secret値は含みません。

```json
{
  "scenario_id": "github-pr-e2e-...",
  "source_branch": "e2e/phase6-...",
  "base_branch": "e2e/phase6-...",
  "task_spec": "specs/tasks/_e2e-phase6-....md",
  "task_id": "phase6-e2e-...",
  "target_branch": "agent/phase6-e2e-...",
  "run1": {
    "workflow_url": "...",
    "sha": "...",
    "conclusion": "success",
    "pr_url": "..."
  },
  "run2": {
    "workflow_url": "...",
    "sha": "...",
    "conclusion": "success",
    "reused_pr_url": "..."
  },
  "pr_count": 1,
  "cleanup": {"status": "COMPLETED"},
  "result": "PASS"
}
```

## Acceptance

```text
Run 1
Production workflow -> Real Codex -> Commit -> Push -> Real PR

Run 2
same work unit rerun -> existing PR reconciliation -> same PR reused
```

終了コード0、workflow/jobs success、remote branch/commit/PR実在、PR identity/body/label PASS、Run 2 PR count 1、Fake Codex/GitHubなしをAcceptanceとします。

## Production Gap policy

E2Eを通すProduction workaroundは追加しません。Gapはresult reportへ次の構造で保存します。

```text
PRODUCTION_GAP: <id>
Contract:
Observed:
Evidence:
Impact:
Required Production Change:
```

Harness assertion/command failureは通常の`errors`へ、Production Contract不足は`production_gap`へ分離します。
