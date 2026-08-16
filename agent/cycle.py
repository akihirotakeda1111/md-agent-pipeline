"""Local autonomous core: Codex → scope → validation → bounded repair.

Does not commit, push, or open pull requests.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.classify import FailureClass, classify_output, classify_validation
from agent.codex_runner import CodexRunResult, Executor, run_codex
from agent.config import AgentConfig, load_config
from agent.errors import AgentError
from agent.gitutil import (
    assert_clean_worktree,
    capture_snapshot,
    collect_changes,
    working_tree_diff_text,
)
from agent.repair import build_repair_prompt, can_attempt_repair
from agent.scope import ScopeCheckResult, check_scope
from agent.select import select_next_task
from agent.spec import SpecTask, TaskSpec, parse_spec
from agent.state import (
    ExecutionState,
    ExecutionStatus,
    apply_transition,
    init_state,
    new_execution_state,
    read_state,
    state_file_path,
    write_state,
)
from agent.validation import ValidationRecord, run_validation_text


@dataclass
class CycleResult:
    outcome: str
    spec_id: str
    task_id: str | None
    base_sha: str | None
    state: ExecutionState
    scope: ScopeCheckResult | None = None
    validations: list[ValidationRecord] = field(default_factory=list)
    classification: FailureClass | None = None
    repair_attempts: int = 0
    message: str = ""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "spec_id": self.spec_id,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "state": self.state.to_json_dict(),
            "scope": None if self.scope is None else self.scope.to_json_dict(),
            "validations": [record.to_json_dict() for record in self.validations],
            "classification": None if self.classification is None else self.classification.value,
            "repair_attempts": self.repair_attempts,
            "message": self.message,
        }


def persist(
    repo_root: Path | str, state: ExecutionState, config: AgentConfig | None = None
) -> None:
    write_state(state_file_path(repo_root, state.task_id, config=config), state)


def load_or_init_state(
    spec: TaskSpec,
    repo_root: Path | str,
    *,
    config: AgentConfig | None = None,
) -> ExecutionState:
    path = state_file_path(repo_root, spec.id, config=config)
    if path.exists():
        return read_state(path)
    return init_state(spec, repo_root, config=config)


def run_final_verification(
    spec: TaskSpec,
    *,
    repo_root: Path | str,
    config: AgentConfig | None = None,
    env: Mapping[str, str] | None = None,
) -> list[ValidationRecord]:
    cfg = config or load_config()
    return run_validation_text(
        spec.final_verification,
        repo_root=repo_root,
        task_id=spec.id,
        timeout_seconds=cfg.validation.timeout_seconds,
        env=env,
    )


def run_task_cycle(
    spec: TaskSpec | Path | str,
    *,
    repo_root: Path | str,
    config: AgentConfig | None = None,
    env: Mapping[str, str] | None = None,
    executor: Executor | None = None,
    state: ExecutionState | None = None,
    persist_state: bool = True,
) -> CycleResult:
    cfg = config or load_config()
    root = Path(repo_root)
    parsed = spec if isinstance(spec, TaskSpec) else parse_spec(spec)
    snapshot = capture_snapshot(root)
    if state is not None:
        current = state
    elif persist_state:
        current = load_or_init_state(parsed, root, config=cfg)
    else:
        path = state_file_path(root, parsed.id, config=cfg)
        current = read_state(path) if path.exists() else new_execution_state(parsed)
    selected = select_next_task(parsed, current)
    if selected is None:
        return _final_verify_if_ready(parsed, current, root, cfg, env, persist_state)
    # Uncommitted files from earlier tasks in this work unit are expected because
    # this phase does not commit. Any other dirty tree is fail-closed.
    if cfg.validation.require_clean_worktree and not current.completed_tasks:
        assert_clean_worktree(snapshot)

    current = _enter_implementing(current, selected)
    if persist_state:
        persist(root, current, cfg)

    implement = run_codex(parsed, selected, repo_root=root, config=cfg, env=env, executor=executor)
    return _after_codex(
        parsed,
        selected,
        current,
        root,
        cfg,
        env,
        executor,
        snapshot.base_sha,
        implement,
        persist_state,
    )


def _final_verify_if_ready(
    spec: TaskSpec,
    state: ExecutionState,
    root: Path,
    cfg: AgentConfig,
    env: Mapping[str, str] | None,
    persist_state: bool,
) -> CycleResult:
    current = state
    if current.state is ExecutionStatus.TASK_COMPLETED:
        current = apply_transition(current, ExecutionStatus.FINAL_VALIDATING)
        if persist_state:
            persist(root, current, cfg)
    if current.state is not ExecutionStatus.FINAL_VALIDATING:
        return CycleResult(
            outcome=current.state.value,
            spec_id=spec.id,
            task_id=None,
            base_sha=None,
            state=current,
            message="no selectable task",
        )
    records = run_final_verification(spec, repo_root=root, config=cfg, env=env)
    failed = next((record for record in records if not record.passed), None)
    if failed is None:
        if persist_state:
            persist(root, current, cfg)
        return CycleResult(
            outcome="FINAL_VERIFICATION_PASSED",
            spec_id=spec.id,
            task_id=None,
            base_sha=None,
            state=current,
            validations=records,
            message="final verification passed; PR is not created in this phase",
        )
    classification = classify_validation(failed)
    target = (
        ExecutionStatus.FAILED
        if classification is FailureClass.ENVIRONMENT_FAILURE
        else ExecutionStatus.ESCALATED
    )
    current = apply_transition(
        current,
        target,
        last_validation=failed.command,
        last_result="FAILED",
    )
    if persist_state:
        persist(root, current, cfg)
    return CycleResult(
        outcome=target.value,
        spec_id=spec.id,
        task_id=None,
        base_sha=None,
        state=current,
        validations=records,
        classification=classification,
        message="final verification failed",
    )


def _enter_implementing(state: ExecutionState, task: SpecTask) -> ExecutionState:
    current = state
    if current.state is ExecutionStatus.PENDING:
        current = apply_transition(current, ExecutionStatus.RUNNING, current_task=task.id)
        return apply_transition(current, ExecutionStatus.IMPLEMENTING)
    if current.state is ExecutionStatus.RUNNING:
        return apply_transition(current, ExecutionStatus.IMPLEMENTING, current_task=task.id)
    if current.state is ExecutionStatus.TASK_COMPLETED:
        return apply_transition(current, ExecutionStatus.IMPLEMENTING, current_task=task.id)
    if current.state is ExecutionStatus.VALIDATING:
        return apply_transition(current, ExecutionStatus.IMPLEMENTING, current_task=task.id)
    if current.state is ExecutionStatus.IMPLEMENTING:
        return current
    raise AgentError.policy_violation(
        f"cannot start implementation from {current.state.value}",
        code="INVALID_TRANSITION",
    )


def _after_codex(
    spec: TaskSpec,
    task: SpecTask,
    state: ExecutionState,
    root: Path,
    cfg: AgentConfig,
    env: Mapping[str, str] | None,
    executor: Executor | None,
    base_sha: str,
    implement: CodexRunResult,
    persist_state: bool,
) -> CycleResult:
    changes = collect_changes(root, base_sha)
    state_rel = Path(cfg.state.directory).as_posix() + f"/{spec.id}.json"
    changes = tuple(change for change in changes if state_rel not in change.paths)
    scope = check_scope(spec, changes)
    if not scope.allowed:
        state = apply_transition(
            state,
            ExecutionStatus.SCOPE_VIOLATION,
            current_task=task.id,
            last_result="FAILED",
        )
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome="SCOPE_VIOLATION",
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            classification=FailureClass.ESCALATION_REQUIRED,
            repair_attempts=state.repair_attempts,
            message=f"SCOPE_VIOLATION: {', '.join(scope.violation_paths)}",
        )

    if implement.exit_code != 0 and not scope.changed_paths:
        classification = classify_output(
            stdout=implement.stdout,
            stderr=implement.stderr,
            binary="codex",
            exit_code=implement.exit_code,
        )
        target = (
            ExecutionStatus.FAILED
            if classification is FailureClass.ENVIRONMENT_FAILURE
            else ExecutionStatus.ESCALATED
        )
        state = apply_transition(state, target, current_task=task.id, last_result="FAILED")
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome=target.value,
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            classification=classification,
            repair_attempts=state.repair_attempts,
            message="codex exited non-zero without in-scope changes",
        )

    state = apply_transition(state, ExecutionStatus.VALIDATING, current_task=task.id)
    if persist_state:
        persist(root, state, cfg)
    return _validate_and_maybe_repair(
        spec, task, state, root, cfg, env, executor, base_sha, scope, persist_state
    )


def _validate_and_maybe_repair(
    spec: TaskSpec,
    task: SpecTask,
    state: ExecutionState,
    root: Path,
    cfg: AgentConfig,
    env: Mapping[str, str] | None,
    executor: Executor | None,
    base_sha: str,
    scope: ScopeCheckResult,
    persist_state: bool,
) -> CycleResult:
    records = run_validation_text(
        task.validation,
        repo_root=root,
        task_id=task.id,
        timeout_seconds=cfg.validation.timeout_seconds,
        env=env,
    )
    failed = next((record for record in records if not record.passed), None)
    if failed is None:
        completed = list(state.completed_tasks)
        if task.id not in completed:
            completed.append(task.id)
        state = apply_transition(
            state,
            ExecutionStatus.TASK_COMPLETED,
            current_task=task.id,
            completed_tasks=completed,
            last_validation=records[-1].command,
            last_result="PASSED",
        )
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome="TASK_COMPLETED",
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            repair_attempts=state.repair_attempts,
            message="validation passed",
        )

    classification = classify_validation(failed)
    if classification is FailureClass.ENVIRONMENT_FAILURE:
        state = apply_transition(
            state,
            ExecutionStatus.FAILED,
            current_task=task.id,
            last_validation=failed.command,
            last_result="FAILED",
        )
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome="FAILED",
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            classification=classification,
            repair_attempts=state.repair_attempts,
            message="environment failure is not sent to repair",
        )

    if classification is FailureClass.ESCALATION_REQUIRED:
        state = apply_transition(
            state,
            ExecutionStatus.ESCALATED,
            current_task=task.id,
            last_validation=failed.command,
            last_result="FAILED",
        )
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome="ESCALATED",
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            classification=classification,
            repair_attempts=state.repair_attempts,
            message="validation failure requires escalation",
        )

    limit = spec.repair_attempt_limit
    if not can_attempt_repair(limit, state.repair_attempts):
        state = apply_transition(
            state,
            ExecutionStatus.ESCALATED,
            current_task=task.id,
            last_validation=failed.command,
            last_result="FAILED",
        )
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome="ESCALATED",
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            classification=classification,
            repair_attempts=state.repair_attempts,
            message="repair_attempt_limit reached",
        )

    state = apply_transition(
        state,
        ExecutionStatus.IMPLEMENTING,
        current_task=task.id,
        repair_attempts=state.repair_attempts + 1,
        last_validation=failed.command,
        last_result="FAILED",
    )
    if persist_state:
        persist(root, state, cfg)
    prompt = build_repair_prompt(
        spec,
        task,
        repo_root=root,
        failed=failed,
        diff_text=working_tree_diff_text(root, base_sha),
    )
    repair_run = run_codex(
        spec,
        task,
        repo_root=root,
        config=cfg,
        env=env,
        executor=executor,
        prompt=prompt,
    )
    return _after_codex(
        spec, task, state, root, cfg, env, executor, base_sha, repair_run, persist_state
    )
