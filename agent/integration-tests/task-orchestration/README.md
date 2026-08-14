# Phase 2 Task Spec fixtures

`specs/tasks/TEMPLATE.md`、`agent/README.md`、`agent/schemas/task-spec.schema.json`を正式な契約として作成したフィクスチャです。

## 正式な構造

Frontmatter:

- `schema_version`: `1`
- `id`, `title`, `status`, `base_branch`, `target_branch`
- `allowed_paths`: 1件以上のstring配列
- `forbidden_paths`: string配列（任意）
- `repair_attempt_limit`, `review_attempt_limit`: 0以上のinteger
- 正常系の`status`はテンプレートに合わせて`PENDING`

Markdown:

- 必須H1は`Objective`、`Non-Goals`、`Forbidden Actions`、`Architecture Invariants`、`Tasks`、`Final Verification`
- Task見出しは`## <task-id>: <title>`
- Task内の必須H3は`Requirement`、`Acceptance Criteria`、`Validation`
- dependencyはTask見出し直下の`depends_on: task-1, task-2`

## 期待結果

| File | Expected | Primary coverage |
|---|---|---|
| `valid-minimal.PASS.md` | PASS | 最小正常系、単一Task、retry下限0 |
| `valid-multi-task-dependencies.PASS.md` | PASS | 定義順、依存解決、決定論的選択 |
| `valid-path-edge-cases.PASS.md` | PASS | glob、dotfile、空白、Unicode |
| `path-absolute.PASS.md` | PASS | 現行schemaがabsolute pathを受理すること |
| `path-parent-traversal.PASS.md` | PASS | 現行schemaが`..`を受理すること |
| `path-overlap.PASS.md` | PASS | allow/forbid同一値を受理すること |
| `path-duplicate.PASS.md` | PASS | 配列内重複を受理すること |
| `invalid-yaml.INVALID_SPEC.md` | INVALID_SPEC | malformed YAML |
| `missing-frontmatter.INVALID_SPEC.md` | INVALID_SPEC | Frontmatterなし |
| `missing-required-field-title.INVALID_SPEC.md` | INVALID_SPEC | 必須field欠落 |
| `wrong-type-repair-limit.INVALID_SPEC.md` | INVALID_SPEC | integerにstring |
| `wrong-type-allowed-paths.INVALID_SPEC.md` | INVALID_SPEC | arrayにstring |
| `unsupported-schema-version.INVALID_SPEC.md` | INVALID_SPEC | schema_version 2 |
| `zero-tasks.INVALID_SPEC.md` | INVALID_SPEC | Task 0件 |
| `task-missing-requirement.INVALID_SPEC.md` | INVALID_SPEC | Requirement欠落 |
| `task-missing-acceptance-criteria.INVALID_SPEC.md` | INVALID_SPEC | Acceptance Criteria欠落 |
| `task-missing-validation.INVALID_SPEC.md` | INVALID_SPEC | Validation欠落 |
| `missing-final-verification.INVALID_SPEC.md` | INVALID_SPEC | Final Verification欠落 |
| `dependency-missing.INVALID_SPEC.md` | INVALID_SPEC | 未知Taskへの依存 |
| `dependency-circular.INVALID_SPEC.md` | INVALID_SPEC | dependency cycle |
| `duplicate-task-id.INVALID_SPEC.md` | INVALID_SPEC | Task ID重複 |
| `negative-repair-limit.INVALID_SPEC.md` | INVALID_SPEC | retry下限境界外 |
| `path-empty.INVALID_SPEC.md` | INVALID_SPEC | pathの`minLength`違反 |

absolute path、parent traversal、重複、allow/forbid overlapを拒否したい場合、現行schemaまたは追加policy validationの強化が必要です。このセットでは現在の実装挙動を期待値としています。

## task-selection期待値

- `valid-minimal.PASS.md`: 初期選択は`setup`
- `valid-multi-task-dependencies.PASS.md`: `prepare` → `implement` → `verify`
- INVALID_SPECではstate初期化とtask-selectionを実行しない

## 実装済みCLIとの結合テスト

`run.py`は次を呼び出します。

```text
python agent/scripts/parse-spec.py <spec.md>
python agent/scripts/validate-spec.py <spec.md>
python agent/scripts/init-state.py --spec <spec.md> --overwrite
python agent/scripts/update-state.py --task-id <frontmatter.id> --to RUNNING
python agent/scripts/select-task.py --spec <spec.md>
```

実行コマンドは下記です。

```text
python agent/integration-tests/task-orchestration/run.py
```

- parseとvalidateは全フィクスチャに実行します。
- validate成功時だけinit-state、update-state、select-taskを実行します。
- 正常系は5コマンドすべての成功、異常系はvalidateの非ゼロ終了を期待します。
- command別のexit code、stdout、stderrはCSV/JSONへ保存します。
- stateを更新するため、テスト用worktreeでの実行を推奨します。
- `valid-multi-task-dependencies.PASS.md`では、`completedTasks`を更新しながら
  `prepare` → `implement` → `verify` → `ALL_COMPLETED`を検証します。
- 各Taskについて`IMPLEMENTING` → `VALIDATING` → `TASK_COMPLETED`と遷移し、
  selectionの`task_id`と`reason`を明示的にassertします。
- `--overwrite`でstateを初期化するため、同じworktreeで再実行できます。
