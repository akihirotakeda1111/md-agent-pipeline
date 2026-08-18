# Runtime State

このディレクトリは Orchestrator-owned の **ephemeral runtime metadata** です。

- Task Spec 本文へ runtime 情報を書き込みません
- 実体は `.agent/state/<task-id>.json`（Phase 2 以降）
- Runtime Codex は編集しません
- GitHub Actions 再実行の Resume ソースにはしません
- GitHub Actions では work unit を最初から再実行します
- ローカル実行では同一 workspace の実行中制御に利用してよい
- MVP では Git へ commit しません
