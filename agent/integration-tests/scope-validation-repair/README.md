# Phase 4 Scope / Validation / Repair integration tests

Phase 2形式のTask Specを入力に、実Phase 2 Parser / Validator / Task Selector、実Phase 3 Codex Runner、実Phase 4 Scope Enforcement / Validation / Failure Classification / bounded Repairを通す独立結合テストです。

## 実行境界

```text
fixtures/*.md
  -> parse-spec.py / validate-spec.py
    -> init-state.py / update-state.py / select-task.py
  -> git init / baseline commit (test setup)
  -> invoke_phase4.py
       -> in-memory Fake Codex injection
       -> Cases 01-07 / 09: run_task_cycle() once
       -> Case 08: run_work_unit() (production task loop + Final Verification)
  -> filesystem snapshot assertions
```

成功判定、retry、state transitionはOrchestratorの責務です。Fake Codexの自己申告は成功根拠にしません。Cases 01-07 / 09 は本番CLI `run-task.py` と同じく `run_task_cycle()` を1回だけ呼びます。内側のbounded repairはcycleに含まれます。Case 08 だけ本番 `run_work_unit()`（CLI `run-work-unit.py`）を使い、task完了後の Final Verification まで進めます。adapter内で `run_task_cycle()` を自前loopしてはいけません。

## 構成

```text
scope-validation-repair/
|-- README.md
|-- run.py
|-- cases.json
|-- fake_codex.py
|-- integration/
|   `-- invoke_phase4.py
|-- fixtures/
|-- workspaces/
|-- expected/
`-- reports/
```

## Cases

| # | Case | observation | 主な境界 |
|---|---|---|---|
| 01 | normal-success | PASS | initial scope -> validation（1× `run_task_cycle`） |
| 02 | scope-violation | SCOPE_VIOLATION | validationを一度も実行せず停止 |
| 03 | repair-success | PASS | validation fail -> repair -> scope -> validation pass |
| 04 | repair-scope-violation | SCOPE_VIOLATION | repair後の禁止path変更を検出 |
| 05 | repair-limit | ESCALATED -> ESCALATION_REQUIRED | 上限到達後はCodexを増やさない |
| 06 | environment-failure | ENVIRONMENT_FAILURE | validation失敗をENVIRONMENT_FAILUREに分類し、repairしない |
| 07 | escalation-required | ESCALATED -> ESCALATION_REQUIRED | AGENT_REPAIRABLEなvalidation失敗がrepair上限でESCALATEDになる。FakeのIMPLEMENTATION_BLOCKED申告は成功根拠にしない |
| 08 | final-validation-failure | ESCALATED -> ESCALATION_REQUIRED | task成功後のFinal Verification失敗。本番 `run_work_unit()` のみ |
| 09 | state-scope-violation | SCOPE_VIOLATION | `.agent/state/leaked.json` を本番scopeが検出。harness snapshotには出ない |

Case 05の本番 `outcome` は `ESCALATED` です。adapterは `message` から `REPAIR_LIMIT_REACHED` を推論しません。

`.agent/state/{spec.id}.json` は現在 production `run_task_cycle()` が scope 対象から外します。このsuiteは `.agent/state/leaked.json` のような **別ファイル** へのCodex変更だけを見ます。`.agent/state/**` 全体がCodex変更から保護されていることまでは保証しません。これは既知のProduction Gapです。adapterでは回避・模倣しません。

## 実行

repository rootから実行します。

```powershell
python agent/integration-tests/scope-validation-repair/run.py
```

`integration/invoke_phase4.py` だけがPhase 4とのadapterです。adapterは次を行います。

```text
# Cases 01-07 / 09
run_task_cycle(spec, repo_root=workspace, config=in-memory Fake Codex, env=secret-filtered)

# Case 08 only (`cases.json` work_unit + `--work-unit`)
run_work_unit(spec, repo_root=workspace, report_dir=temp, config=in-memory Fake Codex,
              env=secret-filtered, persist_state=False)
```

`--task` はselector overrideではありません。Phase 2 `select-task.py` が返したtask idを、Cases 01-07 / 09 では `CycleResult.task_id` と照合します。Case 08 の最終cycleは `task_id=None`（Final Verification）なので、`WorkUnitReport.completed_tasks` / `current_task` と照合します。

stdout JSONは `CycleResult` / `WorkUnitReport` の実値を正規化した次のキーを返します。

- `status` (`outcome` / `classification` からの写像。`message` は使わない)
- `repair_attempts`
- `task_id`
- `outcome`
- `classification`
- `violation_paths` (`CycleResult.scope.violation_paths` の実値。scope未実施時および `WorkUnitReport` は `[]`)

Phase 2 parserやtask selectionをadapter内に再実装してはいけません。adapter内で `while run_task_cycle` のような独自outer loopを組んではいけません。

## Acceptance invariants (this suite)

- workspaceは毎回baselineから一時directoryへcopyする。
- copy後にgit initとbaseline commitを行う（production cycleがGit snapshotを使うため）。
- changed pathsはCodex申告ではなくworkspace snapshotからassertする（`.git/` と `.agent/state/` は観察対象外）。
- `.agent/state/` 配下のCodex変更は snapshot ではなく `CycleResult.scope.violation_paths` でassertする。
- selected taskはPhase 2 selectorと、Cases 01-07 / 09 では `CycleResult.task_id`、Case 08 では `WorkUnitReport.completed_tasks` / `current_task` の両方でassertする。
- `status` と `repair_attempts` はcycle / work unit実値からassertする。
- scope violation時にvalidation sentinelが存在しない。
- GitHub Actions、branch、commit、push、PR操作は行わない。
- Fake Codexへhost secrets（`CODEX_API_KEY`、`API_KEY`、token系）を渡さない。

## Deferred

次は今回のAcceptance対象外です。

- `validation_attempts`
- event count / event ordering
- scope-before-validation trace assertion
- `.agent/state/{spec.id}.json` へのCodex変更のscope検出（productionがこのpathをscope対象外にしている既知Gap。adapterでは埋めない）

## Fake Codex

このsuiteは実Codex CLI smokeを実装しません。外部AIの非決定性・認証・APIコストと切り離して、本番のPhase 2-4経路（Parser / Selector / Runner / Scope / Validation / Repair）を検証するため、Fake Codexだけを使います。
