# Phase 6 Git / PR / Observability integration tests

Phase 6仕様を Source of Truth として、本番の `run_work_unit()` / `run_delivery()` と `.github/workflows/agent-execute.yml` を通す独立結合テストです。Real Codex と Real GitHub API、Real GitHub Actions は起動しません。実 Git（一時 repository / bare remote）と Fake Codex / Fake GitHub で、Commit / Push / PR / Reconciliation / 通知 / 観測契約を検証します。

通常利用者は pytest を直接操作せず、repository root から `run.py` を実行します。

## 実行境界

```text
fixtures/specs/*.md
  -> GitRepo.create（一時 clone + bare remote）
  -> integration/invoke_phase6.py
       -> run_work_unit()     # execute job 相当
       -> worktree reset/clean # deliver の fresh checkout 相当
       -> run_delivery()      # deliver job 相当
  -> 実 Git 状態 / FakeGitHub 記録 / stdout JSONL events
  -> reports/phase6-result.json + reports/pytest.xml
```

本番 Production 自体が execute job と deliver job の 2 段です。Adapter は同じ規則で 2 入口を接続するだけで、新しい retry / classification / notification policy は持ちません。`run_phase6_flow()` はテスト DTO 上の合成であり、Production API ではありません。

## 構成

```text
git-pr-observability/
|-- README.md
|-- run.py
|-- cases.json
|-- requirements.txt
|-- pytest.ini
|-- integration/
|   |-- invoke_phase6.py      # Production binding（唯一の adapter）
|   |-- conftest.py
|   |-- common.py
|   |-- harness/              # Fake Codex / Fake GitHub / GitRepo / observations
|   `-- test_*.py
|-- fixtures/
|   |-- specs/                # 本番 Task Spec schema の fixture
|   `-- github/               # 既存 PR JSON（work-unit marker / head.ref / base.ref）
`-- reports/                  # phase6-result.json / pytest.xml
```

`workspaces/` と `expected/` は Phase 3〜4 と同じ境界の説明用です。ケースごとの mutable workspace や golden snapshot は置きません。観測は実行時の Git / event / GitHub request から取ります。

## Production 経路

```text
execute
Task Spec -> Task Selection -> Codex -> Scope -> Validation -> bounded Repair
-> all Tasks -> Final Verification -> report.json + changes.patch

deliver
report/Spec 照合 -> digest -> GitHub reconciliation -> base/clean
-> apply -> 実 Git diff -> Scope -> manifest -> Final Verification
-> commit -> push -> Pull Request
```

workflow YAML は構造解析のみです。execute は `contents: read` と `CODEX_API_KEY`、deliver は GitHub write 権限で `CODEX_API_KEY` なし。checkout は両 job とも `persist-credentials: false` です。push 認証は Orchestrator の git サブプロセスへ限定注入します。

## Cases

`cases.json` が Mandatory 01〜40 と追加 Contract を管理します。`--list` でタイトル一覧が出ます。

| IDs | Area | 主な確認 |
|---|---|---|
| 01〜05 | work unit | 複数 Task 順、memory state、欠落 state からの Task 1 再開、bounded Repair、Scope |
| 06〜18 | delivery | happy path の commit/push/PR、binding mismatch、manifest、deliver 側 FV |
| 19〜23 | git safety | dirty tree、force/amend/rebase/history rewrite なし（実 Git graph） |
| 24〜28 | reconciliation | 同一 work unit PR の reuse、不正 marker / head / base は reuse しない |
| 29〜35 | failure / notification | FAILED / ESCALATED、Issue or PR comment、label、mention |
| 36〜40 | isolation | execute に GitHub write なし、deliver に Codex 秘密なし |
| W01〜W05 | workflow | permissions、`CODEX_API_KEY` 配置、empty `GITHUB_TOKEN`、persist-credentials |
| F01〜F02 | failure flow | 本番 execute→deliver で通知・label・Job Summary まで到達 |
| O01〜O03 | observability | 必須イベントの部分順序、reuse で delivery validation なし、Repair Attempts 累積 |

Observability はイベント列の完全一致を要求しません。必須イベントの存在と、契約上重要な部分順序だけを見ます。

```text
FINAL_VALIDATION_PASSED
< DELIVERY_VALIDATION_STARTED
< DELIVERY_VALIDATION_PASSED
< PR_CREATED
```

reuse では `DELIVERY_VALIDATION_STARTED` / `DELIVERY_VALIDATION_PASSED` が無く、PR create / patch apply / commit 増加も無いことを副作用から確認します。

`Repair Attempts` は work unit 全体の累積回数です。現在 Task の回数ではありません。PR body / Job Summary / `report.json` は同じ値を持ちます。Task 切替では reset しません。

## 実行

repository root から実行します。`run.py` が pytest / PyYAML / Git / Production workflow / `create_driver()` を preflight し、未接続・skip・workflow 欠落を成功扱いしません。

```powershell
python -m pip install -r agent/integration-tests/git-pr-observability/requirements.txt
python agent/integration-tests/git-pr-observability/run.py
```

Case 一覧:

```powershell
python agent/integration-tests/git-pr-observability/run.py --list
```

個別 case（repeatable）:

```powershell
python agent/integration-tests/git-pr-observability/run.py --case 17
python agent/integration-tests/git-pr-observability/run.py --case W01 --case O03
```

Acceptance は終了コード 0、かつ failure / error / skip がすべて 0 です。結果は次に集約されます。

```text
reports/phase6-result.json
reports/pytest.xml
```

## Adapter

`integration/invoke_phase6.py` だけが Production との接続点です。

```text
create_driver()
  -> run_work_unit(spec, repo_root, report_dir, env, executor)
  -> run_delivery(spec, repo_root, report_dir, github)
```

Adapter の責務は DTO 変換、Fake の wrapping、execute→deliver の GHA 相当 composition、job 間の env restore です。Scope / Validation / Repair / Delivery / Reconciliation / failure classification を再実装してはいけません。

- `FINAL_VERIFICATION_PASSED` → テスト DTO の `PASSED`
- `PR_CREATED` → テスト DTO の `READY`
- mention は `AgentConfig.notification.mention` へ注入（`run_delivery(..., mention=)` は使わない）
- 観測は Production stdout の JSONL を parse する（`emit()` の差し替えはしない）

## Fake / Harness

- `ScriptedCodex`: 呼出し順に scripted response を返す。prompt から `task_id` / `stage` を parse しない
- `FakeGitHub`: 設定済み response を返し、API request を記録する
- `GitRepo`: 一時 repository と bare remote。Git safety の Source of Truth は HEAD / commit graph / remote ref
- `ObservationLog`: event / GitHub request の部分順序

Fake は成功判定をしません。Validation 失敗は実 Production validation（fixture の `python app/check_exists.py ...`）で起こします。

## Acceptance invariants (this suite)

- Production Contract が Source of Truth。テスト DTO のために Production API を広げない。
- 一時 Git repository はケースごとに作り、リポジトリ本体は変更しない。
- Git safety は argv ではなく実 Git 状態から検証する。
- credential isolation の主証拠は workflow YAML（W01〜W05）と execute が GitHub write を呼ばないこと。
- execute 相当の env に GitHub write token を残さず、deliver 相当の env に `CODEX_API_KEY` を残さない。Adapter は job 終了後に process env を戻す。
- FAILED / ESCALATED の機械可読 code は `DeliveryResult.code`。message 文字列の parse で復元しない。
- Real Codex API key、本番 GitHub credential、外部 GitHub access は不要。Fake 値以外の secret を渡さない。

## Required environment

- Python 3.11 以上
- pytest 8.x、PyYAML 6.x（`requirements.txt`）
- Git 2.x
- Production repository の通常 test dependencies

## Deferred

次は今回の Acceptance 対象外です。

- CodeRabbit、async review、review classification / repair loop（Phase 7）
- Real Codex 再検証（Phase 3 / 5）
- Real GitHub Actions E2E（Phase 5 の github-actions suite）
- production deployment
- durable DB / S3 state、checkpoint resume
- automatic rebase / merge
- 全 subprocess への process-runner DI（Git safety は実 Git 状態、credential は workflow contract）
