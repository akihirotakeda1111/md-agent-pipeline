"""Local autonomous core: Codex → scope → validation → bounded repair.

Does not commit, push, or open pull requests.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agent.classify import FailureClass, classify_output, classify_validation
from agent.codex_runner import (
    CodexRunResult,
    Executor,
    attach_codex_api_key,
    build_post_codex_diagnostic,
    detach_codex_api_key,
    emit_codex_diagnostic,
    run_codex,
)
from agent.config import AgentConfig, load_config
from agent.errors import AgentError
from agent.events import (
    CODEX_COMPLETED,
    CODEX_STARTED,
    FINAL_VALIDATION_STARTED,
    REPAIR_STARTED,
    SCOPE_CHECK_PASSED,
    SCOPE_CHECK_STARTED,
    SCOPE_VIOLATION,
    TASK_STARTED,
    VALIDATION_FAILED,
    VALIDATION_PASSED,
    VALIDATION_STARTED,
    emit,
)
from agent.gitutil import (
    assert_clean_worktree,
    capture_snapshot,
    collect_changes,
    working_tree_diff_text,
)
from agent.repair import build_repair_prompt, can_attempt_repair
from agent.scope import ScopeCheckResult, check_scope, validate_spec_scope_policy
from agent.select import select_next_task
from agent.spec import SpecTask, TaskSpec, parse_spec
from agent.state import (
    ExecutionState,
    ExecutionStatus,
    StateFileFingerprint,
    apply_transition,
    assert_current_state_regular_or_absent,
    current_state_relpath,
    fingerprint_state_file,
    init_state,
    new_execution_state,
    read_state,
    state_file_path,
    write_state,
)
from agent.validation import ValidationRecord, run_validation_text

REPAIR_ATTEMPT_LIMIT = "REPAIR_ATTEMPT_LIMIT"
_ESCALATED_FAILURE_CLASSES = frozenset(
    {
        FailureClass.AGENT_REPAIRABLE,
        FailureClass.ESCALATION_REQUIRED,
    }
)


class CycleOutcome(StrEnum):
    TASK_COMPLETED = "TASK_COMPLETED"
    FINAL_VERIFICATION_PASSED = "FINAL_VERIFICATION_PASSED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"


_SUCCESS_CYCLE_OUTCOMES = frozenset(
    {
        CycleOutcome.TASK_COMPLETED,
        CycleOutcome.FINAL_VERIFICATION_PASSED,
    }
)


def _as_cycle_outcome(value: object) -> CycleOutcome:
    if isinstance(value, CycleOutcome):
        return value
    if isinstance(value, StrEnum):
        raise AgentError.invalid_input(
            f"invalid cycle outcome: {value!r}",
            code="INVALID_CYCLE_RESULT",
        )
    if isinstance(value, str):
        try:
            return CycleOutcome(value)
        except ValueError:
            pass
    raise AgentError.invalid_input(
        f"invalid cycle outcome: {value!r}",
        code="INVALID_CYCLE_RESULT",
    )


def _as_failure_class(value: object) -> FailureClass | None:
    if value is None:
        return None
    if isinstance(value, FailureClass):
        return value
    if isinstance(value, str):
        try:
            return FailureClass(value)
        except ValueError:
            pass
    raise AgentError.invalid_input(
        f"invalid cycle failure class: {value!r}",
        code="INVALID_CYCLE_RESULT",
    )


def validate_cycle_result(result: CycleResult) -> None:
    if not isinstance(result.outcome, CycleOutcome):
        raise AgentError.invalid_input(
            f"cycle outcome is not CycleOutcome: {result.outcome!r}",
            code="INVALID_CYCLE_RESULT",
        )
    if result.code is not None and (not isinstance(result.code, str) or result.code == ""):
        raise AgentError.invalid_input(
            "cycle result code must be a non-empty string when set",
            code="INVALID_CYCLE_RESULT",
        )
    if result.outcome in _SUCCESS_CYCLE_OUTCOMES:
        if result.failure_class is not None:
            raise AgentError.invalid_input(
                f"{result.outcome.value} must not set a failure class",
                code="INVALID_CYCLE_RESULT",
            )
        if result.code is not None:
            raise AgentError.invalid_input(
                f"{result.outcome.value} must not set a code",
                code="INVALID_CYCLE_RESULT",
            )
        return
    if result.outcome is CycleOutcome.FAILED:
        if result.failure_class is not FailureClass.ENVIRONMENT_FAILURE:
            raise AgentError.invalid_input(
                "FAILED cycle result requires ENVIRONMENT_FAILURE",
                code="INVALID_CYCLE_RESULT",
            )
        return
    if result.outcome is CycleOutcome.ESCALATED:
        if result.failure_class not in _ESCALATED_FAILURE_CLASSES:
            raise AgentError.invalid_input(
                "ESCALATED cycle result requires AGENT_REPAIRABLE or ESCALATION_REQUIRED",
                code="INVALID_CYCLE_RESULT",
            )
        if (
            result.failure_class is FailureClass.AGENT_REPAIRABLE
            and result.message == "repair_attempt_limit reached"
            and result.code != REPAIR_ATTEMPT_LIMIT
        ):
            raise AgentError.invalid_input(
                "repair limit cycle result requires code REPAIR_ATTEMPT_LIMIT",
                code="INVALID_CYCLE_RESULT",
            )
        return
    if result.outcome is CycleOutcome.SCOPE_VIOLATION:
        if result.failure_class is not FailureClass.ESCALATION_REQUIRED:
            raise AgentError.invalid_input(
                "SCOPE_VIOLATION cycle result requires ESCALATION_REQUIRED",
                code="INVALID_CYCLE_RESULT",
            )
        return
    raise AgentError.invalid_input(
        f"unsupported cycle outcome: {result.outcome!r}",
        code="INVALID_CYCLE_RESULT",
    )


@dataclass
class CycleResult:
    outcome: CycleOutcome
    spec_id: str
    task_id: str | None
    base_sha: str | None
    state: ExecutionState
    scope: ScopeCheckResult | None = None
    validations: list[ValidationRecord] = field(default_factory=list)
    failure_class: FailureClass | None = None
    repair_attempts: int = 0
    message: str = ""
    code: str | None = None

    def __post_init__(self) -> None:
        self.outcome = _as_cycle_outcome(self.outcome)
        self.failure_class = _as_failure_class(self.failure_class)
        validate_cycle_result(self)

    def to_json_dict(self) -> dict[str, Any]:
        validate_cycle_result(self)
        return {
            "outcome": self.outcome.value,
            "spec_id": self.spec_id,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "state": self.state.to_json_dict(),
            "scope": None if self.scope is None else self.scope.to_json_dict(),
            "validations": [record.to_json_dict() for record in self.validations],
            "classification": None if self.failure_class is None else self.failure_class.value,
            "repair_attempts": self.repair_attempts,
            "message": self.message,
            "code": self.code,
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
    assert_current_state_regular_or_absent(path)
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
    validate_spec_scope_policy(parsed, cfg.runtime_edit_policy)
    rest_env, api_key = detach_codex_api_key(env, api_key_env=cfg.codex.api_key_env)
    snapshot = capture_snapshot(root)
    unsafe = _reject_unsafe_current_state(
        parsed, root, cfg, snapshot.base_sha, provided_state=state
    )
    if unsafe is not None:
        return unsafe
    if state is not None:
        current = state
    elif persist_state:
        current = load_or_init_state(parsed, root, config=cfg)
    else:
        current = new_execution_state(parsed)
    selected = select_next_task(parsed, current)
    if selected is None:
        return _final_verify_if_ready(parsed, current, root, cfg, rest_env, persist_state)
    # Uncommitted files from earlier tasks in this work unit are expected because
    # this phase does not commit. Any other dirty tree is fail-closed.
    if cfg.validation.require_clean_worktree and not current.completed_tasks:
        assert_clean_worktree(snapshot)

    current = _enter_implementing(current, selected)
    if persist_state:
        persist(root, current, cfg)

    emit(
        TASK_STARTED,
        f"starting {selected.id}",
        task_id=parsed.id,
        state=current.state.value,
        extra={"spec_task": selected.id},
    )
    emit(
        CODEX_STARTED,
        "codex implementation started",
        task_id=parsed.id,
        state=current.state.value,
        extra={"spec_task": selected.id},
    )
    pre_fingerprint = _preflight_current_state(root, parsed, cfg)
    if pre_fingerprint is None:
        return _state_tampered_result(
            parsed,
            selected,
            current,
            snapshot.base_sha,
            current_state_relpath(parsed.id, cfg),
        )
    implement = _invoke_codex_with_fingerprint(
        parsed,
        selected,
        current,
        root,
        cfg,
        snapshot.base_sha,
        pre_fingerprint,
        lambda: run_codex(
            parsed,
            selected,
            repo_root=root,
            config=cfg,
            env=attach_codex_api_key(rest_env, api_key, api_key_env=cfg.codex.api_key_env),
            executor=executor,
            stage="implementation",
            attempt=0,
        ),
    )
    if isinstance(implement, CycleResult):
        return implement
    emit(
        CODEX_COMPLETED,
        "codex implementation completed",
        task_id=parsed.id,
        state=current.state.value,
        extra={"spec_task": selected.id, "exit_code": implement.exit_code},
    )
    return _after_codex(
        parsed,
        selected,
        current,
        root,
        cfg,
        rest_env,
        api_key,
        executor,
        snapshot.base_sha,
        implement,
        persist_state,
        pre_fingerprint,
        stage="implementation",
        attempt=0,
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
        raise AgentError.policy_violation(
            f"cannot run final verification from {current.state.value}",
            code="INVALID_TRANSITION",
        )
    emit(
        FINAL_VALIDATION_STARTED,
        "final verification started",
        task_id=spec.id,
        state=current.state.value,
    )
    records = run_final_verification(spec, repo_root=root, config=cfg, env=env)
    failed = next((record for record in records if not record.passed), None)
    if failed is None:
        if persist_state:
            persist(root, current, cfg)
        return CycleResult(
            outcome=CycleOutcome.FINAL_VERIFICATION_PASSED,
            spec_id=spec.id,
            task_id=None,
            base_sha=None,
            state=current,
            validations=records,
            repair_attempts=current.repair_attempts,
            message="final verification passed",
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
        outcome=_as_cycle_outcome(target.value),
        spec_id=spec.id,
        task_id=None,
        base_sha=None,
        state=current,
        validations=records,
        failure_class=classification,
        repair_attempts=current.repair_attempts,
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
    api_key: str | None,
    executor: Executor | None,
    base_sha: str,
    implement: CodexRunResult,
    persist_state: bool,
    pre_fingerprint: StateFileFingerprint,
    *,
    stage: str,
    attempt: int,
) -> CycleResult:
    emit(
        SCOPE_CHECK_STARTED,
        "scope check started",
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id},
    )
    state_rel = current_state_relpath(spec.id, cfg)
    post_fingerprint = fingerprint_state_file(state_file_path(root, spec.id, config=cfg))
    if pre_fingerprint != post_fingerprint:
        return _state_tampered_result(spec, task, state, base_sha, state_rel)
    changes = collect_changes(root, base_sha)
    changes = tuple(change for change in changes if state_rel not in change.paths)
    scope = check_scope(spec, changes, cfg.runtime_edit_policy)
    if not scope.allowed:
        emit(
            SCOPE_VIOLATION,
            f"SCOPE_VIOLATION: {', '.join(scope.violation_paths)}",
            task_id=spec.id,
            state=ExecutionStatus.SCOPE_VIOLATION.value,
            extra={"spec_task": task.id},
        )
        state = apply_transition(
            state,
            ExecutionStatus.SCOPE_VIOLATION,
            current_task=task.id,
            last_result="FAILED",
        )
        if persist_state:
            persist(root, state, cfg)
        return CycleResult(
            outcome=CycleOutcome.SCOPE_VIOLATION,
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            failure_class=FailureClass.ESCALATION_REQUIRED,
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
            outcome=_as_cycle_outcome(target.value),
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            failure_class=classification,
            repair_attempts=state.repair_attempts,
            message="codex exited non-zero without in-scope changes",
        )

    emit(
        SCOPE_CHECK_PASSED,
        "scope check passed",
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id},
    )
    state = apply_transition(state, ExecutionStatus.VALIDATING, current_task=task.id)
    if persist_state:
        persist(root, state, cfg)
    return _validate_and_maybe_repair(
        spec,
        task,
        state,
        root,
        cfg,
        env,
        api_key,
        executor,
        base_sha,
        scope,
        persist_state,
        implement=implement,
        stage=stage,
        attempt=attempt,
    )


def _emit_zero_exit_validation_diagnostic(
    *,
    implement: CodexRunResult,
    scope: ScopeCheckResult,
    stage: str,
    attempt: int,
    api_key: str | None,
) -> None:
    if implement.exit_code != 0:
        return
    secrets = [api_key] if api_key else []
    emit_codex_diagnostic(
        build_post_codex_diagnostic(
            exit_code=implement.exit_code,
            changed_paths=scope.changed_paths,
            stage=stage,
            attempt=attempt,
            final_message=implement.final_response,
            secrets=secrets,
        )
    )


def _validate_and_maybe_repair(
    spec: TaskSpec,
    task: SpecTask,
    state: ExecutionState,
    root: Path,
    cfg: AgentConfig,
    env: Mapping[str, str] | None,
    api_key: str | None,
    executor: Executor | None,
    base_sha: str,
    scope: ScopeCheckResult,
    persist_state: bool,
    *,
    implement: CodexRunResult,
    stage: str,
    attempt: int,
) -> CycleResult:
    emit(
        VALIDATION_STARTED,
        "validation started",
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id},
    )
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
        emit(
            VALIDATION_PASSED,
            "validation passed",
            task_id=spec.id,
            state=state.state.value,
            extra={"spec_task": task.id},
        )
        return CycleResult(
            outcome=CycleOutcome.TASK_COMPLETED,
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            repair_attempts=state.repair_attempts,
            message="validation passed",
        )

    _emit_zero_exit_validation_diagnostic(
        implement=implement,
        scope=scope,
        stage=stage,
        attempt=attempt,
        api_key=api_key,
    )
    emit(
        VALIDATION_FAILED,
        failed.command,
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id},
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
            outcome=CycleOutcome.FAILED,
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            failure_class=classification,
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
            outcome=CycleOutcome.ESCALATED,
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            failure_class=classification,
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
            outcome=CycleOutcome.ESCALATED,
            spec_id=spec.id,
            task_id=task.id,
            base_sha=base_sha,
            state=state,
            scope=scope,
            validations=records,
            failure_class=classification,
            repair_attempts=state.repair_attempts,
            code=REPAIR_ATTEMPT_LIMIT,
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
    emit(
        REPAIR_STARTED,
        "bounded repair started",
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id, "attempt": state.repair_attempts},
    )
    prompt = build_repair_prompt(
        spec,
        task,
        repo_root=root,
        failed=failed,
        diff_text=working_tree_diff_text(root, base_sha),
        runtime_policy=cfg.runtime_edit_policy,
    )
    emit(
        CODEX_STARTED,
        "codex repair started",
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id, "attempt": state.repair_attempts},
    )
    pre_fingerprint = _preflight_current_state(root, spec, cfg)
    if pre_fingerprint is None:
        return _state_tampered_result(
            spec,
            task,
            state,
            base_sha,
            current_state_relpath(spec.id, cfg),
        )
    repair_run = _invoke_codex_with_fingerprint(
        spec,
        task,
        state,
        root,
        cfg,
        base_sha,
        pre_fingerprint,
        lambda: run_codex(
            spec,
            task,
            repo_root=root,
            config=cfg,
            env=attach_codex_api_key(env, api_key, api_key_env=cfg.codex.api_key_env),
            executor=executor,
            prompt=prompt,
            stage="repair",
            attempt=state.repair_attempts,
        ),
    )
    if isinstance(repair_run, CycleResult):
        return repair_run
    emit(
        CODEX_COMPLETED,
        "codex repair completed",
        task_id=spec.id,
        state=state.state.value,
        extra={"spec_task": task.id, "exit_code": repair_run.exit_code},
    )
    return _after_codex(
        spec,
        task,
        state,
        root,
        cfg,
        env,
        api_key,
        executor,
        base_sha,
        repair_run,
        persist_state,
        pre_fingerprint,
        stage="repair",
        attempt=state.repair_attempts,
    )


def _reject_unsafe_current_state(
    spec: TaskSpec,
    root: Path,
    cfg: AgentConfig,
    base_sha: str,
    *,
    provided_state: ExecutionState | None,
) -> CycleResult | None:
    """Inspect current state with lstat before any load/persist/Codex."""
    path = state_file_path(root, spec.id, config=cfg)
    fingerprint = fingerprint_state_file(path)
    if not fingerprint.exists or fingerprint.file_type == "regular":
        return None
    current = provided_state if provided_state is not None else new_execution_state(spec)
    selected = select_next_task(spec, current)
    if selected is None:
        selected = spec.tasks[0]
    return _state_tampered_result(
        spec, selected, current, base_sha, current_state_relpath(spec.id, cfg)
    )


def _invoke_codex_with_fingerprint(
    spec: TaskSpec,
    task: SpecTask,
    state: ExecutionState,
    root: Path,
    cfg: AgentConfig,
    base_sha: str,
    pre_fingerprint: StateFileFingerprint,
    invoke: Callable[[], CodexRunResult],
) -> CodexRunResult | CycleResult:
    """Run Codex then always compare fingerprints, even if Codex raises."""
    caught: Exception | None = None
    result: CodexRunResult | None = None
    try:
        result = invoke()
    except Exception as exc:
        caught = exc
    post_fingerprint = fingerprint_state_file(state_file_path(root, spec.id, config=cfg))
    if pre_fingerprint != post_fingerprint:
        return _state_tampered_result(
            spec, task, state, base_sha, current_state_relpath(spec.id, cfg)
        )
    if caught is not None:
        raise caught
    assert result is not None
    return result


def _preflight_current_state(
    root: Path, spec: TaskSpec, cfg: AgentConfig
) -> StateFileFingerprint | None:
    fingerprint = fingerprint_state_file(state_file_path(root, spec.id, config=cfg))
    if fingerprint.exists and fingerprint.file_type != "regular":
        return None
    return fingerprint


def _state_tampered_result(
    spec: TaskSpec,
    task: SpecTask,
    state: ExecutionState,
    base_sha: str,
    state_rel: str,
) -> CycleResult:
    emit(
        SCOPE_VIOLATION,
        f"STATE_TAMPERED: {state_rel}",
        task_id=spec.id,
        state=ExecutionStatus.SCOPE_VIOLATION.value,
        extra={"spec_task": task.id, "code": "STATE_TAMPERED"},
    )
    try:
        state = apply_transition(
            state,
            ExecutionStatus.SCOPE_VIOLATION,
            current_task=task.id,
            last_result="FAILED",
        )
    except AgentError:
        pass
    scope = ScopeCheckResult(
        allowed=False,
        changed_paths=(state_rel,),
        violation_paths=(state_rel,),
        reason="STATE_TAMPERED",
    )
    return CycleResult(
        outcome=CycleOutcome.SCOPE_VIOLATION,
        spec_id=spec.id,
        task_id=task.id,
        base_sha=base_sha,
        state=state,
        scope=scope,
        failure_class=FailureClass.ESCALATION_REQUIRED,
        repair_attempts=state.repair_attempts,
        code="STATE_TAMPERED",
        message=f"STATE_TAMPERED: {state_rel}",
    )
