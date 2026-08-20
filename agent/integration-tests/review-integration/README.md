# Phase 7 CodeRabbit review integration tests

Phase 7仕様を Source of Truth として、本番の `prepare_review()` / `run_review()` と `.github/workflows/agent-review.yml` を通す独立結合テストです。Real Codex と Real GitHub API、Real CodeRabbit、Real GitHub Actions は起動しません。実 Git（一時 repository / bare remote）と Fake Classifier / Fake Codex / Fake GitHub で、wake-up 再取得、pre-filter、分類、Policy、bounded review repair、収束、観測契約を検証します。

通常利用者は pytest を直接操作せず、repository root から `run.py` を実行します。

## 実行境界

```text
fixtures/specs/*.md + fixtures/github/*.json
  -> GitRepo.create（一時 clone + bare remote）
  -> integration/invoke_phase7.py
       -> prepare_review()   # agent-review.yml prepare job 相当
       -> run_review()       # review job 相当
  -> 実 Git 状態 / FakeGitHub 記録 / stdout JSONL events
  -> reports/phase7-result.json + reports/pytest.xml
```

本番 Production 自体が prepare job と review job の 2 段です。Adapter は同じ規則で 2 入口を接続するだけで、新しい classification / policy / scope / validation / convergence / Git 判定は持ちません。`Phase7Driver.run_review()` はテスト DTO 上の合成であり、Production API ではありません。

## 構成

```text
review-integration/
|-- README.md
|-- run.py
|-- cases.json
|-- requirements.txt
|-- pytest.ini
|-- integration/
|   |-- invoke_phase7.py      # Production binding（唯一の adapter）
|   |-- conftest.py
|   |-- common.py
|   |-- harness/              # Fake Classifier / Fake Codex / Fake GitHub / GitRepo / observations
|   `-- test_*.py
|-- fixtures/
|   |-- specs/                # 本番 Task Spec schema の fixture
|   `-- github/               # CodeRabbit feedback JSON（id / actor / body / path / head_sha）
`-- reports/                  # phase7-result.json / pytest.xml
```

ケースごとの mutable workspace や golden snapshot は置きません。観測は実行時の Git / event / GitHub request から取ります。

## Production 経路

```text
GitHub event（wake-up のみ）
  -> prepare_review
       actor / PR identity / work-unit / fork / HEAD 再取得
  -> run_review
       collect CodeRabbit terminal on current HEAD (Checks + commit statuses)
       SKIPPED / failure family -> ESCALATED (classifier / Codex / git write なし)
       collect current CodeRabbit feedback
       -> deterministic pre-filter
       -> Structured Output classifier + schema validation
       -> deterministic policy
       -> ACTIONABLE?  no -> ignore / escalate / IN_REVIEW / READY_FOR_HUMAN
                    yes -> Codex fix -> Scope -> all Task Validation -> FV
                         -> commit -> push
       READY は CODERABBIT_COMPLETED + current HEAD + 未処理 ACTIONABLE なし のみ
```

Event payload だけをレビュー全体と見なしません。PR number は wake-up event と API 再取得結果を照合し、矛盾時は fail-closed です。classifier 結果は必ず Policy を通ります。Codex は実装エンジンであり、GitHub / classifier credential を受け取りません。

workflow YAML は構造解析のみです。`agent-review.yml` は execute workflow と分離し、`pull_request_target` を使わず、checkout は `persist-credentials: false` です。workflow-level concurrency は置かず、review job だけが `needs.prepare.outputs.pull_number` で同一 PR を直列化します（`cancel-in-progress: false`）。prepare は並列のままです。Secret は workflow / job 全体へ置かず、Orchestrator step（`run-review.py`）だけが `CODEX_API_KEY` と `REVIEW_CLASSIFIER_API_KEY` を受け取ります。subprocess 隔離は W06 が確認します。

## Cases

`cases.json` が R01〜H03 を管理します。`--list` でタイトル一覧が出ます。

| IDs | Area | 主な確認 |
|---|---|---|
| R01〜R04F | intake / prefilter | wake-up 再取得、actor、PR number / work-unit / HEAD、obsolete head、forbidden path |
| R03N | identity | event PR number ≠ API PR number。classifier / Codex / commit / push なし。READY にしない |
| R05〜R11 | classifier / policy | schema、confidence、5 enum、allowed / referenced paths |
| R12〜R16 | repair | Codex、Scope、全 Task Validation、FV、commit/push、attempt limit |
| R17〜R21 | identity / convergence | duplicate、edited revision、pending current-HEAD、READY |
| R25〜R30 | terminal | COMPLETED+0/NON_ACTIONABLE → READY、COMPLETED+ACTIONABLE → repair、SKIPPED → ESCALATED、terminalなし+0 → IN_REVIEW、old HEAD無視 |
| R22〜R24 | observability | 必須イベントの存在と有意な部分順序 |
| W01〜W07 | workflow / security | 非同期 trigger、concurrency、最小権限（review の checks: read / statuses: read）、checkout、credential 配置、Codex Action の bot allowlist、subprocess 隔離 |
| H01〜H03 | harness | 実 Git、dumb Fake、JSONL observations |

Observability はイベント列の完全一致を要求しません。必須イベントの存在と、契約上重要な部分順序だけを見ます。仕様が要求する観測は JSONL events、Git 状態、GitHub API 記録です。process argv の記録は Phase 7 の要求ではありません。

```text
REVIEW_RECEIVED
< REVIEW_COLLECTED
< REVIEW_CLASSIFIED
< REVIEW_POLICY_APPLIED
< REVIEW_FIX_STARTED
< REVIEW_FIX_VALIDATION_PASSED
```

READY は current HEAD の CodeRabbit terminal が `CODERABBIT_COMPLETED` で、未処理 ACTIONABLE が無いときだけです。feedback 0件でも COMPLETED なら READY を許可します。duplicate だけでは READY にしません。obsolete head だけのときは `IN_REVIEW` のままにし、READY にしません。SKIPPED は classifier / Codex / Git write なしで ESCALATED です。

## 実行

repository root から実行します。`run.py` が pytest / PyYAML / Git / Production workflow / `create_driver()` を preflight し、未接続・skip・workflow 欠落を成功扱いしません。

```powershell
python -m pip install -r agent/integration-tests/review-integration/requirements.txt
python agent/integration-tests/review-integration/run.py
```

Case 一覧:

```powershell
python agent/integration-tests/review-integration/run.py --list
```

個別 case（repeatable）:

```powershell
python agent/integration-tests/review-integration/run.py --case R12
python agent/integration-tests/review-integration/run.py --case R03N --case W05
```

Acceptance は終了コード 0、かつ failure / error / skip がすべて 0 です。結果は次に集約されます。

```text
reports/phase7-result.json
reports/pytest.xml
```

Harness だけの確認（Production binding 不要）:

```powershell
python -m pytest agent/integration-tests/review-integration/integration/test_harness_contract.py
```

## Adapter

`integration/invoke_phase7.py` だけが Production との接続点です。

```text
create_driver()
  -> prepare_review(repo_root, event_payload, repository, github)
  -> run_review(repo_root, pull_number, head_sha, spec_path, github, classifier, executor, env)
```

Adapter の責務は DTO 変換、Fake の wrapping、prepare→review の GHA 相当 composition、job 間の env restore、stdout JSONL の収集です。Policy / Scope / Validation / Convergence / Git / READY 判定を再実装してはいけません。

- wake-up skip（`should_review=false`）→ テスト DTO の `SKIPPED`
- Production `outcome`（`IN_REVIEW` / `READY_FOR_HUMAN` / `REVIEW_FIX_PUSHED` / `ESCALATED` / `FAILED`）はそのまま status
- classifier は raw transport 出力を返し、Production `classify_review_comment` の schema validation へ渡す
- 観測は Production stdout の JSONL を parse する（`emit()` の差し替えはしない）

## Fake / Harness

- `ScriptedClassifier`: 呼出し順に opaque JSON を返す。classification / policy を決めない
- `ScriptedCodex`: 呼出し順に scripted な file 変更を返す。prompt から policy を parse しない
- `FakeGitHub`: 状態保持。GET は現在の PR / feedback / processed を返し、tracking comment などの write だけ mutate する
- `GitRepo`: 一時 repository と bare remote。Git safety の Source of Truth は HEAD / commit graph / remote ref
- `ObservationLog`: event / GitHub request の部分順序

Fake は成功判定をしません。Validation 失敗は実 Production validation（fixture の `python app/check_content.py ...`）で起こします。

## Acceptance invariants (this suite)

- Production Contract が Source of Truth。テスト DTO のために Production API を広げない。
- 一時 Git repository はケースごとに作り、リポジトリ本体は変更しない。
- Git safety は argv ではなく実 Git 状態から検証する。
- Event は wake-up のみ。レビュー判断は API 再取得後の current PR / HEAD / feedback に基づく。
- event PR number と API 再取得 PR number の不一致は fail-closed。classifier / Codex / commit / push を行わず READY にしない。
- credential isolation の主証拠は workflow YAML（W01〜W05）と Codex subprocess env（W06）。
- Adapter は job 終了後に process env を戻す。
- Real Codex API key、本番 GitHub credential、外部 GitHub / CodeRabbit access は不要。Fake 値以外の secret を渡さない。

## Required environment

- Python 3.11 以上
- pytest 8.x、PyYAML 6.x（`requirements.txt`）
- Git 2.x
- Production repository の通常 test dependencies

## Deferred

次は今回の Acceptance 対象外です。

- Real CodeRabbit / Real OpenAI classifier / Real Codex
- Real GitHub Actions runtime（Phase 5 の github-actions suite）
- production deployment
- automatic rebase / merge
- 全 subprocess への process-runner DI（Validation 成否は実 command と JSONL events、Git safety は実 Git 状態）
