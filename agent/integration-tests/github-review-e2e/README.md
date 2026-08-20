# Phase 7 Real GitHub Review E2E

## Purpose and boundary

Production `agent-execute.yml`でReal PRを作成し、その後のProduction
`agent-review.yml`、Real CodeRabbit、Real OpenAI semantic classifier、Real Codex review
repair、Real GitHub pushを、GitHub上のobservable stateだけで確認するmanual acceptance suiteです。

Phase 6 `github-pr-e2e`の続きですが、suiteは分離しています。
Production workflow、review Policy、convergence判定をコピー・再実装しません。Fake service、shadow
workflow、E2E専用Production trigger、CodeRabbit Autofix、automatic mergeは使用しません。

```text
github-pr-e2e/       Phase 5-6: execute -> Real PR + reconciliation
github-review-e2e/   Phase 7:   Real PR -> asynchronous review -> convergence
```

## 1. Phase 6 E2E structure

Phase 6 suiteの次の構造と粒度を維持しています。

```text
github-review-e2e/
|-- README.md
|-- run.py
|-- self_test.py
|-- requirements.txt
|-- fixtures/
|   `-- phase7-e2e-task.md
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

`run.py`はscenario orchestrationとresult report、`harness/`はGit/GitHubの薄いI/Oと
observable assertion、`fixtures/`はisolated Task Specだけを持ちます。

## 2. Reused helper, fixture, and operating patterns

- CLI引数 + `gh`認証を使う設定方式。Secret値や`.env`は読みません。
- repository default branchから一時cloneし、unique source/base branchを作る方式。
- Task Spec pushまたは既存`workflow_dispatch`からProduction executeを起動する方式。
- workflow ID + branch + SHA + eventでexecute runを一意に探索する方式。
- source/base branchとProduction feature branchを分離する方式。
- head/base/work-unit marker/PR body/label/changed fileをAPIで確認する方式。
- PR countを1に固定し、同じwork-unitの重複PRを許さない方式。
- assertion完了後にPR close、feature branch delete、source branch deleteを行う方式。
- cleanup failureをAcceptance failureと分けたmachine-readable JSON report。
- networkを使わない`self_test.py`。

Task Specはsingle Python fileだけを許可します。Task ValidationとFinal Verificationはbehaviorを
検証するため、CodeRabbit review repair後も同じProduction validationを通せます。レビュー指摘の
文言やclassification結果そのものは固定しません。

## 3. Phase 7 acceptance scenarios

### Scenario A — Review convergence

```text
Production execute
-> Real PR
-> agent-review.yml (`check_run` completed and/or `status`; comments are not subscribed)
-> Production observable terminal on current HEAD
     READY_FOR_HUMAN + agent:ready + workflow completed + PR unmerged
     or ESCALATED + agent:escalated（CodeRabbit SKIPPED 等の仕様どおりの到達含む）
-> [ACTIONABLE repair] Real Codex repair -> linear push -> wait again on new HEAD
```

E2E の終了条件は Production の observable state です。Harness は `coderabbit_terminal() == COMPLETED` を必須にしません。CodeRabbit の Checks / commit status は外部サービスが動いた診断情報として report の `terminal_transports` に残し、READY 判定には使いません。Deliver 直後 HEAD と repair 後 HEAD について、`check_run` / `status` の API 有無、workflow 起動数、prepare 結果、COMPLETED / SKIPPED の観測有無を記録します。

Polling は次の順です。

1. Production ラベル単独では終了しない。current HEAD に対応する review run（prepare の `head_sha`）と structured event が揃って初めて `READY_FOR_HUMAN` / `ESCALATED` / `FAILED` とする
2. repair 前の古い HEAD に付いた terminal / review run は新しい HEAD の終了判定に使わない
3. CodeRabbit の `Review skipped` はレビュー正常完了ではない。current HEAD で `READY_FOR_HUMAN` と同時なら Production bug。仕様どおりの到達は `ESCALATED`
4. terminal（COMPLETED / SKIPPED / FAILED / AMBIGUOUS）があるのに Agent Review が起動しない場合は transport failure
5. `COMPLETED` かつ未処理 feedback なしなど、本来 READY に収束できる条件なのに `IN_REVIEW` のままなら Production bug。workflow success だけでは Production bug にしない。`NONE` / `IN_PROGRESS` は evidence 不足または timeout
6. CodeRabbit の check / commit status / feedback が current HEAD に無い場合は external service blocker
7. 期限超過時は KeyboardInterrupt に頼らず、上記の観測情報付きで自動終了する

Agent Review run の相関は workflow run の `head_sha` や `display_title` を使いません。
`check_run` / `status` 起動では GitHub が default branch SHA を run HEAD として表示することがあるためです。
Harness は E2E 開始前の run ID 集合を baseline にし、その後の新規 `status` / `check_run` run を候補にします。
対象 PR は prepare 出力の `pull_number` と **観測中の current HEAD** で確定します。repair push 後は新しい `current_head` に対する prepare 出力だけを Scenario A 対象にし、古い HEAD の event は ingest しません。
`should_review=false` は正常 skip です。prepare job の failure、または完了後に prepare JSON を取得できない場合は timeout せず Production / Environment 障害として扱います。
`status` と `check_run` が両方来ても、skip では終了せず、review job が実行された run だけを ingest します。片方の in-progress 中は Production terminal で誤終了しません。
timeout 時は run ID / event、run と prepare job の conclusion、prepare 出力取得可否、`should_review` / `reason` / `pull_number` / `head_sha`、Checks / commit status 履歴を report に残します。

古い HEAD に付いた `agent:ready` は成功にしません。最新 review run の structured event が current HEAD に対応していることを確認します。

最低限、次をassertします。

- execute workflowとparse/execute/deliver jobsがsuccess。
- open PRが1つ、head/base/work-unit/allowed fileが一致。
- `agent-review.yml` の prepare/review jobsが完了し、対象がcurrent HEAD。
- READY 経路: structured eventsに`REVIEW_RECEIVED`、`REVIEW_COLLECTED`、`READY_FOR_HUMAN`。feedbackがある場合は`REVIEW_CLASSIFIED`、`REVIEW_POLICY_APPLIED`も存在。`agent:ready`。PR未merge。
- ESCALATED 経路: `REVIEW_RECEIVED`、`REVIEW_ESCALATED`。`agent:escalated`。CodeRabbit SKIPPED 等で Production が仕様どおり到達した場合は E2E 成功。
- ACTIONABLE repairでHEADが変わった場合に限り、`REVIEW_FIX_STARTED`、
  `REVIEW_FIX_VALIDATION_PASSED`、allowed fileだけのlinear push、new HEAD向けの
  次review runを確認。
- READY 経路では current work-unitと最終current HEADに紐づくtracking commentが1つ。
- reportに`coderabbit_terminal`（診断）、`actionable_repair_observed`、`repair_count`、
  `current_head_feedback_converged`を記録。
- 最終状態は`READY_FOR_HUMAN`または`ESCALATED`。PRはopen、未merge、auto-mergeなし。
- 全stateのPR数が1で、重複PRなし。

Scenario Aで Production が `FAILED` へ到達した場合は証拠を report へ保存し、この acceptance は FAIL です。内容を固定してCodeRabbitを誘導する workaroundは追加しません。

### Scenario B — Human comment does not start Agent Review

Scenario Aの同じPRへ、Harnessの実GitHub userから通常commentを1件投稿します。
`@coderabbitai full review` を含む人間コメントは CodeRabbit への指示であり、
`agent-review.yml` の起動条件ではありません。

```text
real human GitHub comment
-> agent-review.yml は issue_comment / review comment を購読しない
-> Agent Review は起動しない
-> classifierなし / Codexなし / pushなし
-> Scenario A の Production terminal state と HEAD を維持
```

途中で coincidental な `check_run` / `status` が走っても Scenario B の失敗にはしません。
コメント event で Agent Review が起動したら ProductionBug です。
投稿物はE2E PR内に閉じ、default cleanupでPRと一緒に閉じます。

## 4. Real / Fake boundary

すべてRealです。

- Real GitHub repository/API/Actions
- Real Production `agent-execute.yml` / `agent-review.yml`
- Real Codex CLI/API credential
- Real CodeRabbit GitHub App
- Real OpenAI review classifier
- Real Git commit/push/PR/comment/label

Harnessはbranch/Task Specの作成、既存workflowの起動、bounded polling、GitHub API evidence、
assertion、cleanupだけを行います。Classification enum、confidence threshold、allowed-path Policy、
processed判定、attempt計算、READY判定はHarnessにありません。`.agent/state/**`は読み書きしません。

## 5. Manual Preconditions

- CodeRabbit GitHub App installed / authorized。
- CodeRabbit auto review enabled。
- CodeRabbit incremental review enabled。
- CodeRabbit `reviews.auto_review.base_branches`が`e2e/phase7-*`の一時base branchを
  review対象に含む。空list（default branchのみ）のままではこのsuiteは
  `ENVIRONMENT_BLOCKER`としてpreflight停止します。
- Production `agent/config.json`の`coderabbit.actor`が実eventで観測したactorに設定済み。
  Harnessは値をhard-codeせず、cloneしたconfigを読み、実feedback actorと照合します。
- Repository Secret `CODEX_API_KEY`。
- Repository Secret `REVIEW_CLASSIFIER_API_KEY`。
- classifier model configが利用可能なpinned modelを参照。
- Production `agent-execute.yml`と`agent-review.yml`がactive。
- GitHub ActionsによるPR作成とnormal feature-branch pushが許可済み。
- branch protectionがE2E feature branchへのnormal pushを妨げない。
- automatic merge disabled。PR単位の`auto_merge`もnullであることを実行中にassertします。
- Harnessの`gh` credentialにsource/feature branch read/write/delete、Actions read/dispatch、
  PR read/close、issue/review comment作成権限がある。
- Harness userは`coderabbit.actor`と異なる。

HarnessはRepository Secretの値も一覧も読みません。Secret preconditionは人間が確認します。

## 6. Cleanup strategy

実行ごとに次をunique化します。

```text
source/base:    e2e/phase7-<unique-id>
Task Spec:      specs/tasks/_e2e-phase7-<unique-id>.md
Task ID:        phase7-e2e-<unique-id>
feature branch: agent/phase7-e2e-<unique-id>
generated file: app/e2e_phase7_<unique_id>.py
```

全assertion/evidence取得後にだけ、次を順番に行います。

1. E2E PRをclose。
2. `agent/phase7-e2e-*` feature branchをdelete。
3. `e2e/phase7-*` source/base branchをdelete。

既存Production branch、他PR、default branch、他labelは変更しません。`--keep-resources`時は
何も削除せず、PR番号とbranchをreportに残します。cleanup failureは
`PASS_CLEANUP_FAILED` / `FAIL_CLEANUP_FAILED`として非ゼロ終了します。

## 7. Bounded timeout strategy

Production workflowへsleep/pollingを追加しません。HarnessだけがAPI stateをpollします。

- `--discovery-timeout-seconds 240`: run/PR/event discovery。
- `--execute-timeout-seconds 1800`: execute workflow完了。
- `--review-timeout-seconds 1800`: Production review terminal または各review run完了。
- `--convergence-timeout-seconds 5400`: initial reviewからREADY/ESCALATEDまでの全体上限。
- `--poll-seconds 10`: API poll間隔。

各loopは`time.monotonic()` deadlineを持ち、無期限wait、Production内wait、固定長の盲目的sleep、
KeyboardInterrupt待ちを行いません。期限超過時は候補 run、prepare 結果、Checks / commit status を
reportへ残し、観測内容から Production bug / transport failure / external service blocker を分類します。

## Source-of-Truth preflight

cloneしたProduction refから次を読み、SHA-256と選択configをreportへ保存します。

- `.agent/bootstrap.md`
- Global Contracts 3件
- Phase 6 / Phase 7 specs
- Production execute/review workflows
- `agent/config.json`
- `.coderabbit.yaml`
- `README.md`

missing file、inactive workflow、unsafe trigger、`pull_request_target`、unbounded workflow wait、
未設定actor/model/track authorはProduction側の問題として停止します。コピーで補いません。

## Install and offline self-test

```powershell
python -m pip install -r agent/integration-tests/github-review-e2e/requirements.txt
python agent/integration-tests/github-review-e2e/self_test.py
```

`self_test.py`はGitHub resourceやnetworkを使わず、fixture rendering、naming、trigger、workflow
contract、assertion、report schema、bounded polling、no-Fake/no-policy-duplicationを確認します。

## Preflight-only

GitHub認証、Production ref、workflow/config/manual setupの機械確認可能部分だけを確認します。
branch、PR、commentは作成しません。

```powershell
python agent/integration-tests/github-review-e2e/run.py `
  --repo OWNER/REPO `
  --preflight-only
```

## Real GitHub execution

```powershell
python agent/integration-tests/github-review-e2e/run.py `
  --repo OWNER/REPO
```

既存`workflow_dispatch`を明示的に使う場合:

```powershell
python agent/integration-tests/github-review-e2e/run.py `
  --repo OWNER/REPO `
  --trigger workflow_dispatch
```

調査用にresourceを残す場合:

```powershell
python agent/integration-tests/github-review-e2e/run.py `
  --repo OWNER/REPO `
  --keep-resources
```

## Result and failure classification

`reports/github-review-e2e-<unique-id>.json`へ次を保存します。

- workflow run IDs/URLs/events/jobs/conclusions/structured events
- PR URL/number/source/base/head SHA history
- CodeRabbit feedback kind/id/actor/path/head association（本文は保存しない）
- repair前後SHA、linear comparison、incremental review evidence
- Deliver/repair HEAD ごとの `check_run` / `status` transport 記録と workflow 起動数
- terminal labels/state、tracking comment ID、PR count、merge/auto-merge状態
- Scenario B human comment does not start Agent Review; HEAD/state preservation
- cleanup結果

回避ロジックは入れず、failureを次のいずれかに分類します。

- `E2E_BUG`: Harness自身の実装/fixture問題。
- `PRODUCTION_BUG`: Production contract/workflow/observable acceptance不一致。
- `ENVIRONMENT_BLOCKER`: auth、permission、Secret/manual setup、branch protection等。
- `EXTERNAL_SERVICE_BLOCKER`: GitHub/CodeRabbit/OpenAI/Codexのtimeout/outage/rate limit等。

Real external review内容は固定しません。人工再現が必要なclassifier failure、attempt limit、scope
violation、conflict、obsolete HEAD等は`review-integration`に残します。

## Stop boundary

このsuiteはPhase 7 E2E資材だけです。Phase 8、automatic merge、deployment、Production feature追加へ
進みません。
