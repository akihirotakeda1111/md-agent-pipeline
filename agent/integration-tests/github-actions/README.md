# Phase 5 GitHub Actions integration tests

本番 workflow `.github/workflows/agent-execute.yml` を対象にした結合テストです。影 YAML や Fake Codex adapter は使いません。

```text
A. Production Workflow Contract
   verify_contract.py → agent-execute.yml

B. Real Push Integration
   01 normal / 02 invalid / 03 feature-branch skip
   → ephemeral branch へ spec を push
   → production jobs
   → 01 の execute は openai/codex-action の sandbox bootstrap のあと
     production Orchestrator（run-work-unit.py → deliver.py）

C. Real workflow_dispatch Integration
   04 dispatch-skip
   → gh workflow run --ref <ephemeral> -f spec_path=...
```

## Contract（GitHub 不要）

リポジトリ root から:

```powershell
python agent/integration-tests/github-actions/integration/verify_contract.py
python -m pytest agent/tests/test_workflow_contract.py agent/tests/test_phase5_harness.py
```

`python -m pytest` でも同じ contract が走ります。

## Real GitHub Integration

`gh` と `git push` に次の権限が必要です。

- **Contents: write** — ephemeral branch の作成、push、削除
- **Actions: read** — workflow run の list / view / watch
- **Actions: write** — `workflow_dispatch`（04）

classic PAT なら `repo` + `workflow`。fine-grained なら Contents を Read and write、Actions を Read and write。ephemeral branch を push し、完了後に削除します。

```powershell
python agent/integration-tests/github-actions/run.py --repo OWNER/REPO
```

個別実行:

```powershell
python agent/integration-tests/github-actions/run.py --repo OWNER/REPO --case 02-invalid-spec
python agent/integration-tests/github-actions/run.py --repo OWNER/REPO --case 03-feature-branch-skip
python agent/integration-tests/github-actions/run.py --repo OWNER/REPO --case 04-dispatch-skip
python agent/integration-tests/github-actions/run.py --repo OWNER/REPO --case 01-normal-success
```

`--keep-branch` で掃除を省略します。失敗時の調査用です。`--report-dir` で出力先を変えられます（省略時は `reports/`）。

実行後は他 suite と同じく次のファイルに全ケースをまとめます。失敗しても行は残し、`run_id` / URL を落とさないようにしています。

```text
reports/results.json
reports/results.csv
reports/<case>.log          # FAIL 時のみ。gh run view --log-failed
```

JSON の各ケースは `status`（PASS/FAIL）、job conclusion、`run_id`、URL、`head_sha`、branch を持ちます。FAIL のときは GitHub の失敗ログを `<case>.log` に保存し、prefix を除いて `cycle_result` を復元します。`CODEX_API_KEY` は値を残さず、有無だけ `codex_api_key_present`（true/false/null）にします。

## Cases

| Case | Trigger | Spec | Codex | 期待 |
|---|---|---|---|---|
| `01-normal-success` | push | 正当 spec、`base_branch` = ephemeral branch | Real Codex | workflow success、Parse spec / Execute task / Deliver が success |
| `02-invalid-spec` | push | 不正 spec | なし | workflow failure、Parse spec failure、Execute / Deliver skipped |
| `03-feature-branch-skip` | push | 正当 spec、`base_branch` = default branch ≠ branch | なし | workflow success、Parse spec success、Execute / Deliver skipped |
| `04-dispatch-skip` | workflow_dispatch `--ref` ephemeral | 専用 fixture を `specs/tasks/_it-*.md` に載せる。`base_branch` = default branch ≠ branch | なし | event=workflow_dispatch、Parse spec success、Execute / Deliver skipped |

01 だけ execute job が始まります。本番 YAML では `openai/codex-action` が prompt なしの sandbox bootstrap を先に走り、そのあと `run-work-unit.py` が Real Codex を起動します。Action は Orchestrator の代替ではありません。bootstrap が失敗すれば execute は Orchestrator まで到達せず FAIL します。01 だけリポジトリ Secret `CODEX_API_KEY` が必要です。deliver job は write token で Commit / PR します。Settings で GitHub Actions の PR 作成を許可してください。

02–04 は execute が始まらないため Secret なしで回せます。bootstrap も走りません。04 の `--ref` に default branch を渡してはいけません。正当 spec だと Real Codex が起動します。

## 構成

```text
github-actions/
|-- README.md
|-- cases.json
|-- run.py
|-- integration/
|   |-- verify_contract.py
|   |-- expectations.py
|   |-- spec_template.py
|   |-- runs.py
|   `-- reporting.py
|-- fixtures/
|   |-- 01-normal-success.PASS.md
|   |-- 02-invalid-spec.INVALID_SPEC.md
|   |-- 03-feature-branch-skip.SKIP.md
|   `-- 04-dispatch-skip.SKIP.md
`-- reports/            # results.json / results.csv / <case>.log
```

01–04 の fixture はテンプレです。harness が unique `task_id` を埋め、worktree 上の `specs/tasks/_it-*.md` に書いて push します。main には残しません。04 は同じ commit の push run と dispatch run が両方立ち得るため、`branch + HEAD SHA + event + run id` で自分が起動した run だけを特定します。

## Deferred

- dispatch-normal（execute は 01 で証明）
- dispatch-invalid（parse 失敗は 02 で証明）
- Commit / Push / Pull Request と Restart / GitHub Reconciliation の Real GitHub 確認は `agent/integration-tests/github-pr-e2e/`
- CodeRabbit
