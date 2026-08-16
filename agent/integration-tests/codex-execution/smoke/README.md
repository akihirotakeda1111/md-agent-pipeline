# Optional Real Codex smoke

親directoryの`run.py --real-codex`を使用します。

実Codex CLIを使用するsmoke testは、Linux環境で実行してください。Windowsを使用する場合は、Windowsネイティブ環境ではなくWSL上のLinux環境で実行します。

`RUN_CODEX_SMOKE_TEST=1`が設定されている場合だけ、実Codex CLIで次の経路を確認します。

- `--real-codex`のみの場合は、デフォルトでCase 01（`01-create-file`）だけを実行します。
- `--real-codex --all-cases`の場合は、Real Codex対象の全ケースを実行します。

Case 01だけを実行する例:

```bash
RUN_CODEX_SMOKE_TEST=1 python3 ../run.py --real-codex
```

全ケースを実行する例:

```bash
RUN_CODEX_SMOKE_TEST=1 python3 ../run.py --real-codex --all-cases
```

```text
Phase 2 Task Spec
→ real parser/schema
→ real task selector
→ real prompt builder/runner
→ real Codex CLI
→ temporary workspace file change
```

通常CIではReal Codexを実行せず、APIコストやネットワーク依存を発生させません。
