# Phase 3 Codex execution integration tests

`task-orchestration/` と同じく、実装済みCLIをsubprocessで呼び出す独立した結合テストです。Phase 3の主Acceptanceとして、Phase 2形式Task SpecからCurrent Taskを選択し、実Prompt Builder・実Codex Runnerを通じてfixture workspaceへ変更が発生するところまで確認します。

## 実行フロー

```text
fixtures/*.md
  → agent/scripts/parse-spec.py
  → agent/scripts/validate-spec.py
  → agent/scripts/init-state.py
  → agent/scripts/update-state.py --to RUNNING
  → agent/scripts/select-task.py
  → invoke_runner.py（test-only executable injection）
  → agent.codex_runner.run_codex
  → Fake Codex executable
  → disposable workspace
  → test-side workspace assertion
```

Parser、Schema Validator、Task Selector、Prompt Builder、Codex Runnerは既存実装をそのまま使用します。このdirectoryにMarkdown parserやtask selectionを再実装しません。

Stateは一時state rootへ、各workspaceは`workspaces/<case>/`から一時directoryへcopyして使用します。リポジトリ本体、Phase 2のfixture/state、baseline workspaceは変更しません。

## 構成

```text
codex-execution/
├── README.md
├── run.py
├── invoke_runner.py
├── fake_codex.py
├── cases.json
├── fixtures/
│   ├── 01-create-file.PASS.md
│   ├── 02-modify-file.PASS.md
│   ├── 03-inspect-then-modify.PASS.md
│   ├── 04-add-function.PASS.md
│   ├── 05-multiple-files.PASS.md
│   ├── 06-no-change.PASS.md
│   └── 07-protected-path.BLOCKED.md
├── workspaces/
├── expected/
├── reports/              # run.pyがresults.json / results.csvを生成
└── smoke/
    └── README.md
```

Unit/component testsは既存の`agent/tests/test_codex_runner.py`等を正とします。このdirectoryへ重複配置しません。

## Integration cases

| Case | Spec | Purpose | Codex | Workspace | Expected |
|---|---|---|---|---|---|
| 01 | `01-create-file.PASS.md` | 新規ファイル作成 | execute | changed | PASS |
| 02 | `02-modify-file.PASS.md` | 既存ファイルの限定変更 | execute | changed | PASS |
| 03 | `03-inspect-then-modify.PASS.md` | repository inspection | execute | changed | PASS |
| 04 | `04-add-function.PASS.md` | source function追加 | execute | changed | PASS |
| 05 | `05-multiple-files.PASS.md` | 1 Taskから複数ファイル変更 | execute | changed | PASS |
| 06 | `06-no-change.PASS.md` | 不要変更の防止 | execute | unchanged | PASS |
| 07 | `07-protected-path.BLOCKED.md` | Contract違反が必要 | blocked | unchanged | BLOCKED |

`Validation`と`Final Verification`はPhase 2 Schema互換性のため各Specに存在しますが、Phase 3 Runtimeは実行しません。各sectionにはsentinel作成commandを置き、結合テストはsentinelが存在しないことをassertします。

workspaceの変更pathと内容はテストコードがassertします。これはAcceptance Testの事後確認であり、Runtime Scope Enforcementではありません。

## 通常実行

リポジトリrootから実行します。

```powershell
python agent/integration-tests/codex-execution/run.py
```

通常実行はFake Codexを使用するため、認証・network・OpenAI APIコストは発生しません。Fakeは実Runnerから受け取ったstdin prompt、argv、environment key、working directoryをJSONL eventとして返し、選択Task markerに対応する決定論的変更を行います。

`invoke_runner.py`はWindowsを含めてFake executableの絶対pathを`CodexConfig.bin`へ注入する薄いテストadapterです。Task Specのparseやprompt構築は再実装せず、既存`parse_spec`、`resolve_task`、`run_codex`を呼びます。productionの`agent/config.json`や`run-codex.py`は変更しません。Real smokeではこのadapterを使わず、実`agent/scripts/run-codex.py`を呼びます。

report形式はPhase 2と同じです。

```text
reports/results.json
reports/results.csv
```

## Optional Real Codex smoke

Real Codexは明示opt-in時だけ実行します。`--real-codex`のみの場合はデフォルトでCase 01を実行し、`--all-cases`を追加すると対象となる全ケースを実行します。

実Codex CLIを使用するテストはLinux環境で実行してください。Windowsを使用する場合は、Windowsネイティブ環境ではなくWSL上のLinux環境で実行します。

```bash
RUN_CODEX_SMOKE_TEST=1 python3 agent/integration-tests/codex-execution/run.py --real-codex

# 全ケースを実行
RUN_CODEX_SMOKE_TEST=1 python3 agent/integration-tests/codex-execution/run.py --real-codex --all-cases
```

公式Codex CLIの導入・認証が必要です。`RUN_CODEX_SMOKE_TEST=1`がない`--real-codex`指定はSKIPとして終了し、通常CIからReal Codexを起動しません。

## Phase 3で行わないこと

- Runtimeのgit diff Scope Enforcement
- Spec内Validation / Final Verificationの実行
- Repair Loop
- Workflow StateのESCALATED更新
- branch / commit / push / PR
- GitHub Actions

Blocked caseはCodexの`IMPLEMENTATION_BLOCKED`申告とworkspace不変だけを確認します。
