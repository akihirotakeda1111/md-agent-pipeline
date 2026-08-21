# コードレビュー／リファクタリング管理票

## 対応状況ダッシュボード

| ID | Priority | 概要 | Status | Owner | Target | Issue / PR | Verification |
|---|---|---|---|---|---|---|---|
| RF-01 | HIGH | Runtime Codexの保護pathをhard policy化 | OPEN | — | Phase 7完了前 | — | 未実施 |
| RF-02 | MEDIUM* | Review trackingの状態モデルを自動Repair再開前に整理 | OPEN | — | 自動Repair有効化前 | — | 一部緩和済み |
| RF-03 | HIGH | Phase 6/7間でTask Spec内容をbind | OPEN | — | Phase 7完了前 | — | 未実施 |
| RF-04 | HIGH | Classifier／Review repairへ正しいTask contextを渡す | OPEN | — | Phase 7完了前 | — | 未実施 |
| RF-05 | MEDIUM | outcome/failure/reportモデルの型付け | OPEN | — | Phase 7完了前推奨 | — | 未実施 |
| RF-06 | MEDIUM | Review/Cycle transaction scriptの分割 | OPEN | — | 段階対応 | — | 未実施 |
| RF-07 | MEDIUM | subprocess security／command policyの集約 | OPEN | — | Phase 7完了前推奨 | — | 未実施 |
| RF-08 | MEDIUM | GitHub API境界のDTO化 | OPEN | — | Phase 7完了後 | — | 未実施 |
| RF-09 | MEDIUM | failure reportingをbest effort化 | OPEN | — | Phase 7完了前 | — | 未実施 |
| RF-10 | MEDIUM | unit/integration/Real E2E構造の整理 | OPEN | — | Phase 7完了後 | — | 未実施 |
| RF-11 | MEDIUM | Task Spec semantic validationの追加 | OPEN | — | Phase 7完了後 | — | 未実施 |
| RF-12 | LOW | config/promptのsource of truth整理 | OPEN | — | MVP後 | — | 未実施 |
| RF-13 | LOW | 未使用／到達不能／旧Phaseコード整理 | OPEN | — | MVP後 | — | 未実施 |
| RF-14 | LOW | Git filename parserのNUL-safe化 | OPEN | — | MVP後 | — | 未実施 |
| RF-15 | LOW | GitHubClient timeout設定の有効化 | OPEN | — | MVP後 | — | 未実施 |

> **Priority注記:** RF-02は現在のMVP構成では`MEDIUM`。自動Repairを有効化する前には`HIGH`として扱う。

## HIGH priority

### RF-01 — Runtime Codexの保護対象pathがhard policyになっていない

- **Status:** `OPEN`
- **対象:** `agent/scope.py::path_is_in_scope`、`agent/cycle.py::_after_codex`、`agent/schemas/task-spec.schema.json`、`agent/codex_runner.py::build_implementation_prompt`
- **コード根拠:** [scope.py L45](agent/scope.py#L45)、[cycle.py L319](agent/cycle.py#L319)、[task-spec.schema.json L7](agent/schemas/task-spec.schema.json#L7)、[codex_runner.py L218](agent/codex_runner.py#L218)
- **現状の問題:** scope判定はTask Specの`allowed_paths`と`forbidden_paths`だけに依存する。`forbidden_paths`はschema上必須ではなく、`.agent/**`、`agent/**`、`.github/**`、`specs/**`を常に拒否するProduction policyがない。さらに`cycle._after_codex()`は`.agent/state/{spec.id}.json`を実差分から無条件に除外する。
- **追加根拠:** Phase 4 integration READMEはexact state fileの除外を既知のProduction Gapとして明記している。[scope-validation-repair README L51](agent/integration-tests/scope-validation-repair/README.md#L51)
- **なぜリファクタリングが必要か:** 不完全または誤ったTask Specによって、Contract上Codexが編集不能なHuman/Orchestrator/Platform-ownedファイルを許可できる。`persist_state=False`でもcurrent Task IDと同名のstate fileはvalidation前に検出されない。
- **推奨方針:** Task Specより優先される小さな`RuntimeEditPolicy`をscope層へ置く。Task Spec parserでもprotected rootとのallow重複を拒否する。local stateについては差分から消さず、Codex実行直前と実行後のdigestを比較し、Orchestrator自身の変更とCodex変更を区別する。
- **変更影響範囲:** Spec、Scope、Cycle、Delivery、Review repair、prompt、Phase 2/4/6/7 tests。
- **対応時期:** Phase 7完了前。
- **関連テスト:** `agent/tests/test_scope.py`、`agent/tests/test_phase6.py::test_deliver_scope_rejects_agent_state_in_patch`、`agent/integration-tests/scope-validation-repair`。
- **追加すべき回帰テスト:** protected pathをTask Specがallowしても拒否するケース、`persist_state=False`でexact state fileを書かれたケース、Orchestratorが正当に書いたlocal stateを誤検知しないケース。
- **壊してはいけないContract:** Architecture Ownership、I-01、I-02、I-05、promptはsecurity boundaryではないこと、Phase 6の「`.agent/state/**`をScope検査から除外しない」。
- **対応メモ:** —

### RF-03 — Phase 6で確定したTask SpecとPhase 7が使うTask Specが内容で結び付いていない

- **Status:** `OPEN`
- **対象:** `agent/pr.py`、`agent/review_prepare.py`、`agent/review_loop.py`、`agent/review_track.py`
- **コード根拠:** [pr.py L10](agent/pr.py#L10)、[review_prepare.py L249](agent/review_prepare.py#L249)、[review_loop.py L189](agent/review_loop.py#L189)、[review_track.py L43](agent/review_track.py#L43)
- **現状の問題:** PR markerとReviewTrackは`spec_id/base_branch/target_branch`しか保持しない。Phase 7はcurrent `pull.head.sha`からTask Specを取得するため、同一PRの後続commitでTask Specのscope、acceptance、validation、attempt limitが変わると、新しい内容が自動修復policyとして使われる。
- **なぜリファクタリングが必要か:** 「1 Task Spec = 1 Work Unit」のidentityが名前とbranchだけで、Human-owned contractの内容にbindされていない。
- **推奨方針:** Delivery時に`spec_path`、`spec_sha256`、必要なら`delivery_base_sha`をWorkUnitReportとPR markerへ記録する。Phase 7はcurrent HEADからのAPI再取得を維持しつつ、delivery時のdigestと不一致ならescalateする。ReviewTrackも同じWorkUnitIdentityへbindする。
- **変更影響範囲:** Report、PR marker schema、reconcile、review prepare、tracking、Phase 6/7 integration、Real E2E。
- **対応時期:** Phase 7完了前。
- **関連テスト:** `agent/tests/test_phase7.py::test_prepare_resolves_spec_from_api_head_not_checkout_tree`、Phase 6 report context mismatch tests。
- **追加すべき回帰テスト:** PR作成後にTask Spec本文だけを変更したHEAD、spec path移動、同じIDで内容が異なるspec、digest一致のreview repair push。
- **壊してはいけないContract:** wake-up payloadをSource of Truthにしないこと、current HEADのAPI再取得、Human-owned Task Spec、既存PRの安全なreconciliation。
- **対応メモ:** —

### RF-04 — Semantic ClassifierとReview repairに渡すTask contextが不十分

- **Status:** `OPEN`
- **対象:** `agent/review_classify.py::_request_body`、`agent/review_loop.py::_apply_review_fix`、`agent/review_prompt.py`
- **コード根拠:** [review_classify.py L92](agent/review_classify.py#L92)、[review_loop.py L445](agent/review_loop.py#L445)、[review_prompt.py L39](agent/review_prompt.py#L39)
- **現状の問題:** Classifier入力にobjective、non-goals、各taskのrequirement/acceptance/validation、final verificationが含まれない。一方でClassifierには`CONFLICTS_WITH_SPEC`を判断させている。修復時はfeedbackの対象に関係なく`spec.tasks[-1]`がCurrent Taskになる。
- **なぜリファクタリングが必要か:** multi-task specで前半taskへのreviewが来ると、Classifierは本来のacceptance criteriaを知らず、Codexには最後のtaskの文脈が渡る。scope内であってもTask Specと意味的に衝突する修正を受け入れる可能性がある。
- **推奨方針:** MVPでは全taskのrequirement/acceptance/validationをClassifierとReview repairへ渡し、最後のtaskをrelevantと仮定しない。将来taskごとのowned pathをモデル化した時点で、通常コードが関連taskを決定する。Codex runnerのlogging用taskとreview contextを分離する。
- **変更影響範囲:** Classifier request、prompt、Codex runner stage model、multi-task tests。
- **対応時期:** Phase 7完了前。
- **関連テスト:** `agent/tests/test_phase7.py::test_classifier_request_uses_structured_output_and_review_key`。現在はmodel/schema/keyのみ確認し、Task Spec内容は確認していない。
- **追加すべき回帰テスト:** task-1へのfeedbackを含むmulti-task spec、task-1 acceptanceと衝突するcomment、複数taskに跨るfeedback。
- **壊してはいけないContract:** Classifierはsemantic classificationのみ、Structured Output、deterministic Policy、original Task Spec、relevant Current Task。
- **対応メモ:** —

## MEDIUM priority

### RF-02 — Review trackingの状態モデルが将来の自動Repairに対して粗い

- **Status:** `OPEN`（一部緩和済み）
- **Priority:** 現在は`MEDIUM`。`review.auto_repair_enabled: true`へ変更する前は`HIGH`として対応必須。
- **対象:** `agent/review_loop.py`、`agent/review_track.py`、`agent/labels.py`
- **コード根拠:** [review_loop.py L212](agent/review_loop.py#L212)、[review_loop.py L350](agent/review_loop.py#L350)、[review_loop.py L414](agent/review_loop.py#L414)、[review_loop.py L707](agent/review_loop.py#L707)、[review_loop.py L770](agent/review_loop.py#L770)、[review_track.py L30](agent/review_track.py#L30)
- **現在実装済みの緩和策:** Productionでは`review.auto_repair_enabled: false`のため、ACTIONABLE feedbackはCodex Repairへ進まず人間へhandoffされる。同一HEADでESCALATED/FAILED/READYになった後は、ReviewTrackの`head_sha`とGitHub terminal labelを使うsticky判定によりterminal outcomeを維持する。`test_same_head_escalated_is_sticky_against_ready`で、ESCALATED後にCodeRabbit COMPLETEDが観測されてもREADYへ戻らないことを検証している。
- **現状の問題:** `ReviewTrack`が保持するfeedback状態は`processed`だけであり、NON_ACTIONABLEとして解決済み、policy escalation済み、修復対象、修復成功済みを型として区別できない。Feature gateを有効にした場合はCodex Repair成功前にidentityとattemptが永続化される。またterminal状態はReviewTrack単独ではなく、`track.head_sha`とGitHub labelの組み合わせに暗黙依存する。
- **なぜリファクタリングが必要か:** 現在のMVP経路で「再実行だけでREADYになる」問題はsticky terminalにより緩和済みである。一方、自動Repairを再開すると、途中失敗、tracking更新とlabel更新の部分成功、push前後のcrash recoveryを`processed`だけで安全に説明しにくい。新しいcase追加時の保守コストと状態不整合リスクが残る。
- **推奨方針:** 自動Repairを有効化する前に、少なくとも`pending_repair`、`resolved_non_actionable`、`resolved_fixed`、`escalated`を区別する。Attempt開始と成功も別状態にし、`resolved_fixed`への遷移はpush成功後に限定する。MVP中は過剰なmigrationを避け、sticky terminalとfeature gateを維持して延期してよい。
- **変更影響範囲:** Tracking comment schema/version、prefilter、policy、repair、convergence、labels、idempotency、integration adapter、Real E2E evidence。
- **対応時期:** Phase 7 MVP完了のblocking itemではない。ただし自動Review Repairを有効化する前に必須。
- **既存テスト:** `agent/tests/test_phase7.py::test_same_head_escalated_is_sticky_against_ready`、auto-repair feature gate tests、classifier failure、scope violation、attempt limit、READY rerun tests、`agent/integration-tests/review-integration/integration/test_repair_git.py`。
- **追加すべき回帰テスト:** Feature gate有効時の修復失敗後の2回目run、commit後push失敗からの再実行、tracking更新成功・label更新失敗の部分成功、human resolution後の明示的な再開。
- **壊してはいけないContract:** bounded review attempt、edited commentの再処理、GitHub durable tracking、current HEAD convergence、sticky terminal、SKIPPED/failureをREADYにしないこと。
- **対応メモ:** 同一HEADのfalse READYという具体的リスクは現実装で緩和済み。残課題は将来の自動Repairに向けた状態モデルの明確化。

### RF-05 — outcome/failure/reportモデルが文字列と重複booleanで競合する

- **Status:** `OPEN`
- **対象:** `agent/workunit.py::WorkUnitReport`、`agent/delivery.py`、`agent/cli.py`
- **コード根拠:** [workunit.py L36](agent/workunit.py#L36)、[workunit.py L92](agent/workunit.py#L92)、[delivery.py L73](agent/delivery.py#L73)、[cli.py L317](agent/cli.py#L317)
- **現状の問題:** `outcome: str`と`final_verification_passed`、`validation_passed`、`scope_allowed`、`classification`、`skip_reason`が同じ状態を別表現で持つ。load時の`bool(payload.get(...))`は文字列`"false"`もTrueにする。report専用schemaとsemantic invariantがない。repair limit時もoutcomeはESCALATEDだがclassificationはAGENT_REPAIRABLEのまま残る。
- **なぜリファクタリングが必要か:** Artifact境界、CLI exit、通知、integration adapterがそれぞれ独自の文字列写像を持ち、case追加時に複数ファイルを同期する必要がある。
- **推奨方針:** `CycleOutcome`、`WorkUnitOutcome`、`DeliveryOutcome`、`ReviewOutcome`を境界別に定義する。booleanはoutcomeから導出する。ReportにJSON Schemaとsemantic constructorを追加し、`FailureClass`と`FailureCode/Reason`を分離する。
- **変更影響範囲:** Cycle、WorkUnit、Delivery、CLI、report schema、adapters、tests。
- **対応時期:** Phase 7完了前を推奨。
- **関連テスト:** `agent/tests/test_phase6.py`のcommit/PR gate、Phase 4 repair-limit integration。
- **壊してはいけないContract:** no commit before validation、no PR before Final Verification、FAILED/ESCALATED分類、artifact digest/context照合。
- **対応メモ:** —

### RF-06 — ReviewとCycleが大きなtransaction scriptになっている

- **Status:** `OPEN`
- **対象:** `agent/review_loop.py::_run_review`、`agent/cycle.py::_after_codex`、`agent/cycle.py::_validate_and_maybe_repair`、`agent/workunit.py::run_work_unit`
- **コード根拠:** [review_loop.py L157](agent/review_loop.py#L157)、[cycle.py L296](agent/cycle.py#L296)、[cycle.py L429](agent/cycle.py#L429)、[workunit.py L120](agent/workunit.py#L120)
- **現状の問題:** `_run_review()`がGitHub取得、identity、terminal、prefilter、Classifier、Policy、tracking、Codex、Git、labelを一つの関数で処理する。Cycleはstate/result構築を繰り返し、repair後に`_after_codex()`へ再帰する。
- **なぜリファクタリングが必要か:** Durable checkpointやfailure case追加のたびにreturn、tracking、event、labelを同時に修正する必要があり、処理順序のバグを生みやすい。
- **推奨方針:** 汎用workflow engineは作らず、Phase固有の`collect/evaluate/persist/repair/converge`へ分割する。Cycleは明示的なbounded loopとtyped transition resultへ変え、result構築を共通化する。
- **変更影響範囲:** Phase 4/6/7の主要テスト。
- **対応時期:** Review側の最小分割はRF-03〜04と同時。RF-02に関わる状態分割は自動Repair有効化前。Cycle全体はPhase 7後。
- **関連テスト:** `test_cycle.py`、`test_phase6.py`、`test_phase7.py`、review integration。
- **壊してはいけないContract:** Event順序、scope-before-validation、bounded repair、Git writeはOrchestratorのみ。
- **対応メモ:** —

### RF-07 — subprocess security policyとvalidation分類が分散している

- **Status:** `OPEN`
- **対象:** `agent/codex_runner.py`、`agent/gitutil.py`、`agent/gitwrite.py`、`agent/validation.py`、`agent/classify.py`
- **コード根拠:** [codex_runner.py L50](agent/codex_runner.py#L50)、[gitutil.py L12](agent/gitutil.py#L12)、[gitwrite.py L16](agent/gitwrite.py#L16)、[validation.py L19](agent/validation.py#L19)、[classify.py L17](agent/classify.py#L17)
- **現状の問題:** GitとValidationがcredential sanitationのためCodex Runnerへ依存する。Validation allowlistには`make`があるがrepairable binariesにはなく、許可された`make`失敗だけがrepair対象外になる。
- **なぜリファクタリングが必要か:** Security-critical policyの責務が逆転し、command追加時にallow/classificationを同期し忘れやすい。
- **推奨方針:** sanitized environmentを共通の`subprocess_env`境界へ移す。Codex key注入はCodex Runnerに残す。Validation commandは`binary → allowed/denied/default failure class`の単一registryにする。
- **変更影響範囲:** Codex、Git、Validation、credential isolation、classification tests。
- **対応時期:** Phase 7完了前を推奨。
- **関連テスト:** `test_codex_runner.py`、`test_validation.py`、`test_classify.py`、Phase 6 write-isolation。
- **壊してはいけないContract:** Codex/Classifier/GitHub credential分離、Validation/Gitへのsecret非継承、unknown failureのfail closed。
- **対応メモ:** —

### RF-08 — GitHub APIのraw dictがdomain層まで漏れている

- **Status:** `OPEN`
- **対象:** `agent/github_api.py`、`agent/pr.py`、`agent/review_collect.py`、`agent/review_prepare.py`
- **コード根拠:** [github_api.py L118](agent/github_api.py#L118)、[pr.py L58](agent/pr.py#L58)、[review_collect.py L43](agent/review_collect.py#L43)、[review_prepare.py L215](agent/review_prepare.py#L215)
- **現状の問題:** Pull、Check、Status、Commentが`dict[str, Any]`のまま渡され、number/head/base/repo/userの検証が複数moduleに存在する。malformed payloadの失敗方法もばらつく。
- **なぜリファクタリングが必要か:** GitHub field変更や別review source追加時の変更範囲が広く、identity validationの差異を作りやすい。
- **推奨方針:** API adapter出口で`PullIdentity`、`PullRef`、`CheckEvidence`、`StatusEvidence`、`CommentRef`程度の小さなDTOへ変換する。全endpointの抽象化は行わない。
- **変更影響範囲:** GitHub client、reconcile、review prepare/collect/terminal、fake clients。
- **対応時期:** Phase 7完了後。
- **関連テスト:** `test_github_api.py`、`test_phase6.py`、`test_review_terminal.py`、`test_phase7.py`。
- **壊してはいけないContract:** API再取得、HEAD/PR/work-unit identityのfail closed、pagination。
- **対応メモ:** —

### RF-09 — failure reporting自体のGitHub障害が元の失敗を隠す

- **Status:** `OPEN`
- **対象:** `agent/delivery.py::run_delivery/_notify`、`agent/review_loop.py::_control_plane_failure`
- **コード根拠:** [delivery.py L166](agent/delivery.py#L166)、[delivery.py L394](agent/delivery.py#L394)、[review_loop.py L750](agent/review_loop.py#L750)
- **現状の問題:** Delivery catch内の`_notify()`が再度失敗するとsummary/result/eventへ到達しない。Reviewもcomment作成はbest effortだが、その前のlabel更新は無防備。
- **なぜリファクタリングが必要か:** GitHub outageが主原因の場合、同じ依存先を使うfailure reportingがoriginal errorとstructured resultを隠す。
- **推奨方針:** Primary resultとlocal summary/eventを先に確定する。GitHub通知はbest effortのsecondary effectとし、通知失敗を別diagnosticとして記録する。
- **変更影響範囲:** Delivery、Review、Labels、Summary、Notification tests。
- **対応時期:** Phase 7完了前。
- **関連テスト:** Phase 6 failure notification tests。Label/comment API failureのテストは未整備。
- **壊してはいけないContract:** FAILED/ESCALATED区別、Job Summary、機械可読event、通知のidempotency。
- **対応メモ:** —

### RF-10 — テストの層は正しいが、Phase monolithとHarness基盤が肥大化している

- **Status:** `OPEN`
- **対象:** `agent/tests/test_phase6.py`、`agent/tests/test_phase7.py`、`agent/integration-tests/github-review-e2e/run.py`、`agent/integration-tests/github-review-e2e/harness/github.py`
- **コード根拠:** [test_phase6.py](agent/tests/test_phase6.py)、[test_phase7.py](agent/tests/test_phase7.py)、[github-review-e2e/run.py L353](agent/integration-tests/github-review-e2e/run.py#L353)、[harness/github.py L472](agent/integration-tests/github-review-e2e/harness/github.py#L472)
- **現状の問題:** Phase 6/7 unit testsとReview Real E2Eのmain/GitHub harnessが巨大化している。Phase 6/7 Real E2E間にはworkflow filter/process helperの同形実装があり、Phase integration adapterもstatus写像やPR markerを再実装する。
- **なぜリファクタリングが必要か:** Production refactor時にContract変更とfixture/adapter変更を区別しにくく、scenario追加が巨大な条件分岐を増やす。
- **推奨方針:** Unit testをproduction module/contract単位へ分割し、Phase acceptance matrixはREADME/case dataとして残す。Real E2Eではtest専用のprocess/workflow/GitHub query基盤だけを共有し、scenario runnerとcleanup contextを分離する。
- **変更影響範囲:** Test organizationのみ。Production importは増やさない。
- **対応時期:** RF-01〜04の回帰テスト追加は今すぐ。構造整理はPhase 7後。
- **関連テスト:** Phase 6/7 unit、integration、Real E2E全体。
- **壊してはいけないContract:** Real E2EはProduction workflowを使用する。CodeRabbit terminal mapperはProductionをimportせず、共有observed fixtureで独立検証する。
- **対応メモ:** —

### RF-11 — Task Specのschema validationと実行可能な意味契約に差がある

- **Status:** `OPEN`
- **対象:** `agent/schemas/task-spec.schema.json`、`agent/spec.py`、`agent/intake.py`
- **コード根拠:** [task-spec.schema.json L25](agent/schemas/task-spec.schema.json#L25)、[spec.py L126](agent/spec.py#L126)、[task-orchestration README L30](agent/integration-tests/task-orchestration/README.md#L30)
- **現状の問題:** `status`は任意の非空文字列でProductionの実行可否に使われない。absolute path、`..`を含むscope pattern、非空だが無効なbranchもparserを通る。
- **なぜリファクタリングが必要か:** typoや完了済みstatusでも実行され、無効なpath/refがGit/Codexまで進んで遅く失敗する。
- **推奨方針:** `TaskSpecStatus`と実行可否を定義する。path patternはrepo-relativeかつ正規化可能、branchは安全なGit refであることをsemantic validatorで確認する。RF-01のprotected path拒否だけは先行する。
- **変更影響範囲:** Schema、Spec、Intake、task-orchestration fixtures/docs。
- **対応時期:** RF-01部分を除きPhase 7後。
- **関連テスト:** `path-absolute.PASS.md`、`path-parent-traversal.PASS.md`は期待値変更が必要。
- **壊してはいけないContract:** Human-owned Task Spec、1 Spec/1 Work Unit、invalid inputではCodexを起動しないこと。
- **対応メモ:** —

## LOW priority / cleanup

### RF-12 — configとpromptのsource of truthが重複している

- **Status:** `OPEN`
- **対象:** `agent/config.json`、`agent/config.py`、`agent/codex_runner.py`、`agent/repair.py`、`agent/review_prompt.py`
- **コード根拠:** [config.json L8](agent/config.json#L8)、[config.py L130](agent/config.py#L130)、[codex_runner.py L537](agent/codex_runner.py#L537)、[repair.py L31](agent/repair.py#L31)
- **現状の問題:** Pinned version、model、actor/context等がconfig、Python defaults、README、error messageに重複する。Task Specのprompt blockもimplementation/repair/reviewで別実装になっている。
- **なぜリファクタリングが必要か:** Versionや制約文を変更するたびに複数箇所の同期が必要になる。
- **推奨方針:** Repository-controlled値はconfig.jsonをsource of truthにする。PromptはTask Spec制約blockだけ共有し、stage固有promptは分離したままにする。
- **変更影響範囲:** Config、Prompt、Tests、Docs。
- **対応時期:** MVP後。
- **関連テスト:** `test_config.py`、`test_codex_runner.py`。
- **壊してはいけないContract:** Version/model pin、user config非依存、promptはsecurity boundaryではないこと。
- **対応メモ:** —

### RF-13 — 古いPhase由来の未使用・到達不能コードが残っている

- **Status:** `OPEN`
- **対象:** `agent/gitwrite.py`、`agent/review_prepare.py`、`agent/intake.py`、`agent/__init__.py`
- **コード根拠:** [gitwrite.py L166](agent/gitwrite.py#L166)、[gitwrite.py L175](agent/gitwrite.py#L175)、[review_prepare.py L87](agent/review_prepare.py#L87)、[intake.py L19](agent/intake.py#L19)、[agent/__init__.py](agent/__init__.py)
- **現状の問題:** `prepare_feature_worktree`、`commits_ahead_of`、`current_branch`、`sender_login`はrepository内参照がない。`IN_FLIGHT_STATUSES`はすべて`STARTABLE_STATUSES`にも含まれ、in-flight block branchは到達不能。Package facadeはPhase 1〜4相当で止まっている。
- **なぜリファクタリングが必要か:** 現在有効なreconciliation設計と過去の設計を区別しにくくする。
- **推奨方針:** 外部利用がないことを確認後に削除する。Intakeはlocal in-flightを許可するかblockするかをContractとして決めてから集合とテストを整理する。
- **変更影響範囲:** 小。
- **対応時期:** MVP後。
- **関連テスト:** `test_intake.py`はRUNNINGをstartableとして固定しているため、先に意図を確定する必要がある。
- **壊してはいけないContract:** GHA rerunでExecution StateをResumeにしないこと、execute jobにGitHub writeを持たせないこと。
- **対応メモ:** —

### RF-14 — Git filename parserがNUL-safeでない

- **Status:** `OPEN`
- **対象:** `agent/gitutil.py`
- **コード根拠:** [gitutil.py L90](agent/gitutil.py#L90)、[gitutil.py L188](agent/gitutil.py#L188)、[gitutil.py L195](agent/gitutil.py#L195)
- **現状の問題:** Git出力を改行、tab、` -> `で解析し、pathに`strip()`を使用するため、空白、tab、改行、特殊renameを含む合法filenameを正確に扱えない。
- **なぜリファクタリングが必要か:** Mechanical scope checkの入力がfilename表現に依存し、path manifestとの不一致や誤検知を起こし得る。
- **推奨方針:** `git diff --name-status -z`、`ls-files -z`、porcelain `-z`へ統一する。
- **変更影響範囲:** GitUtil、Scope、Delivery tests。
- **対応時期:** MVP後。
- **関連テスト:** 特殊filename、rename、untracked pathのfixtureを追加する。
- **壊してはいけないContract:** Git実差分をSource of Truthにすること、renameの旧新path双方を検査すること。
- **対応メモ:** —

### RF-15 — GitHubClientのtimeout設定が実際のrequestに使われない

- **Status:** `OPEN`
- **対象:** `agent/github_api.py::GitHubClient/_default_requester`
- **コード根拠:** [github_api.py L50](agent/github_api.py#L50)、[github_api.py L398](agent/github_api.py#L398)
- **現状の問題:** Constructorは`timeout_seconds`を保存するが、default requesterは常にmodule定数を使用する。
- **なぜリファクタリングが必要か:** 設定可能に見えるinterfaceと実際のruntime動作が異なり、timeout tuningやテストが正しく機能しない。
- **推奨方針:** Requester factoryまたはrequest contextでinstance timeoutを渡す。Custom requesterのtest interfaceは必要以上に広げない。
- **変更影響範囲:** GitHub clientとtestsのみ。
- **対応時期:** MVP後。
- **関連テスト:** `test_github_api.py`へinstance timeout適用テストを追加する。
- **壊してはいけないContract:** GitHub timeoutをENVIRONMENT_FAILUREへ分類すること。
- **対応メモ:** —

## 現状のまま維持すべき設計

- GitHub Actionsを起動、権限、artifact受け渡しに限定し、判定ロジックをPythonへ置く構成。
- Executeのread-only権限とDeliverのGitHub write権限の分離。
- Codex、Classifier、GitHub credentialを別経路にし、Git/Validation subprocessへ継承しない設計。
- Git read inspectionとGit write操作を`gitutil`/`gitwrite`へ分けた構成。
- Deliveryでpatch digest、実差分scope、manifest、Final Verificationを再確認する多層防御。
- Phase 7を別async workflowにし、eventをwake-upに限定してcurrent HEADをGitHub APIから再取得する設計。
- Structured Classifierの後にdeterministic Policyを必ず通す構成。
- `.agent/state`をGHA durable resumeにせず、Git/PRをdurable stateとする原則。
- Real E2E terminal mapperをProductionから独立させ、共有observed fixtureだけで一致を確認する構成。
- CodeRabbit固有処理を`review_*`周辺へ集めている現在の範囲。第二providerがない段階で汎用plugin frameworkは導入しない。

## 技術的負債が集中している3領域

1. **Phase 7 durable review convergence**
   - 現在のMVPはfeature gateとsticky terminalで安全側に寄せているが、自動Repair再開時には`processed`、attempt、escalation、repair成功の意味を分離する必要がある。
2. **ScopeとWork Unit identityのContract境界**
   - Protected pathがTask Spec慣習に依存し、Phase 6からPhase 7へのTask Spec内容がbindされていない。
3. **Cross-phase result/reportと巨大Orchestrator**
   - Outcome文字列、boolean、failure class/codeがCycle、WorkUnit、Delivery、Review、CLI、Harnessへ波及している。

## 推奨実施順序

1. RF-01、RF-03、RF-04の現状失敗経路を再現する回帰テストを追加する。
2. RF-01のhard protected-path policyとexact state file検証を実装する。
3. RF-03のTask Spec digestをWorkUnit、PR、ReviewTrackへbindする。
4. RF-04のClassifierとReview repairのmulti-task contextを修正する。
5. 自動Review Repairを有効化する計画が決まった時点で、RF-02のReviewTrack schemaを未解決／解決／escalatedへ分離する。
6. RF-05のWorkUnitReport/outcome/failure modelを型付けする。
7. RF-07とRF-09を整理する。
8. RF-06のReview loopをstep単位へ分割する。
9. Phase 7安定後にCycle、GitHub DTO、Task Spec semantic validation、test harnessを整理する。
10. 最後にconfig重複、dead code、Git path parser、timeoutをcleanupする。
