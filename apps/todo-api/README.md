# TODO API experiment workload

This directory is a demonstration workload for the MD-driven AI development pipeline. It is the shared, buildable baseline for comparing Phase 6 and Phase 7 under the same initial conditions.

## Baseline boundary

The baseline contains only a Go module, a minimal HTTP server bootstrap, and a placeholder handler. The placeholder intentionally returns `501 Not Implemented`; this is not a completed TODO application.

The paired Task Specs at `specs/tasks/todo-api-phase6.md` and `specs/tasks/todo-api-phase7.md` ask the implementation agent to add the domain model, in-memory storage, CRUD HTTP behavior, validation, JSON errors, and tests. Their implementation requirements are identical; only `id` and `target_branch` differ so GitHub reconciliation cannot reuse one experiment's PR for the other. The baseline intentionally contains none of that business logic and contains no frontend, database, authentication, Docker, cloud, deployment, migration, production secrets, Phase 7 code, CodeRabbit integration, or seeded defects.

## Baseline integrity

Run these commands from the repository root before recording the baseline commit:

```text
go -C apps/todo-api vet ./...
go -C apps/todo-api test ./...
go -C apps/todo-api build ./...
python agent/scripts/validate-spec.py specs/tasks/todo-api-phase6.md
python agent/scripts/validate-spec.py specs/tasks/todo-api-phase7.md
```

Passing these checks proves that the scaffold and Task Spec are valid. It does not prove the TODO acceptance criteria; those become applicable only after an experiment executes the Task Spec.

## Fixed comparison conditions

Record every value below before either run. Do not change the Task Spec or baseline between runs.

| Condition | Fixed value or source |
| --- | --- |
| Baseline commit SHA | Record the commit containing this baseline |
| Implementation requirements | Identical Markdown bodies in `todo-api-phase6.md` and `todo-api-phase7.md` |
| Phase 6 identity | `id: todo-api-phase6`, `target_branch: feature/todo-api-phase6` |
| Phase 7 identity | `id: todo-api-phase7`, `target_branch: feature/todo-api-phase7` |
| Shared base branch | `baseline/todo-api` |
| Codex CLI version | `agent/config.json` → `codex.version` |
| Codex model | `agent/config.json` → `codex.model` |
| Agent configuration | Entire `agent/config.json` at the baseline commit |
| Allowed paths | `apps/todo-api/**` |
| Validation commands | The Task Spec at the baseline commit |
| Repair limit | Task Spec `repair_attempt_limit` |
| Go language version | `go 1.22.0` in this module's `go.mod` |
| Go toolchain | Record the full output of `go version`; use that exact toolchain for both runs |

Also keep the runner OS, relevant environment variables, and invocation method equal where practical. Preserve each run's execution summary and PR URL rather than editing this baseline between runs.

## Experiment protocol

1. Commit this baseline and record its SHA. Optionally create an annotated baseline tag.
2. From that SHA, execute `specs/tasks/todo-api-phase6.md` with the recorded Phase 6 configuration.
3. Independently from the same SHA, execute `specs/tasks/todo-api-phase7.md` with the recorded Phase 7 configuration.
4. Never merge, cherry-pick, copy, or otherwise carry Phase 6 generated code into the Phase 7 branch.
5. Compare outputs only after both independent runs finish.

Phase 6 observes: Task Spec → Codex → Scope → Validation / Repair → Final Verification → Commit / Push / PR → Human review.

Phase 7 observes: Task Spec → Codex → Scope → Validation / Repair → Final Verification → Commit / Push / PR → CodeRabbit → Review Classification → deterministic Policy → review repair → `READY_FOR_HUMAN`.

## Results record

Results do not exist at baseline time. Store or link the final Phase 6 PR, Phase 7 PR, execution summaries, validation evidence, repair counts, review findings, and comparison conclusions in a separate experiment-results document or portfolio entry after both runs. Do not write generated results back into the shared baseline commit.
