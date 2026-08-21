"""Execution state JSON I/O and explicit state machine."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

import jsonschema
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from agent.config import AgentConfig, load_config
from agent.errors import AgentError
from agent.spec import TaskSpec

SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "execution-state.schema.json"

_EXECUTION_STATE_SCHEMA: dict[str, Any] | None = None


@dataclass(frozen=True)
class StateFileFingerprint:
    exists: bool
    file_type: str
    content_sha256: str | None
    symlink_target: str | None


def current_state_relpath(spec_id: str, config: AgentConfig) -> str:
    directory = Path(config.state.directory).as_posix().rstrip("/")
    return f"{directory}/{spec_id}.json"


def fingerprint_state_file(path: Path | str) -> StateFileFingerprint:
    """Fingerprint a current-state file without following symlinks."""
    target = Path(path)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return StateFileFingerprint(
            exists=False,
            file_type="absent",
            content_sha256=None,
            symlink_target=None,
        )
    except OSError as exc:
        raise AgentError.environment_failure(
            f"current state could not be fingerprinted: {target}",
            code="STATE_TAMPERED",
        ) from exc

    mode = info.st_mode
    if stat.S_ISLNK(mode):
        return StateFileFingerprint(
            exists=True,
            file_type="symlink",
            content_sha256=None,
            symlink_target=os.readlink(target),
        )
    if stat.S_ISREG(mode):
        return StateFileFingerprint(
            exists=True,
            file_type="regular",
            content_sha256=_sha256_regular_file(target),
            symlink_target=None,
        )
    if stat.S_ISDIR(mode):
        return StateFileFingerprint(
            exists=True,
            file_type="directory",
            content_sha256=None,
            symlink_target=None,
        )
    return StateFileFingerprint(
        exists=True,
        file_type="other",
        content_sha256=None,
        symlink_target=None,
    )


def assert_current_state_regular_or_absent(path: Path | str) -> None:
    """Fail closed if the current-state path exists and is not a regular file.

    Uses lstat so a symlink to a valid Execution State JSON is rejected before
    any follow-on read or replace.
    """
    fingerprint = fingerprint_state_file(path)
    if fingerprint.exists and fingerprint.file_type != "regular":
        raise AgentError.policy_violation(
            f"current state is not a regular file: {path}",
            code="STATE_TAMPERED",
        )


def _sha256_regular_file(path: Path) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AgentError.environment_failure(
            f"current state could not be read: {path}",
            code="STATE_TAMPERED",
        ) from exc
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    IMPLEMENTING = "IMPLEMENTING"
    VALIDATING = "VALIDATING"
    TASK_COMPLETED = "TASK_COMPLETED"
    FINAL_VALIDATING = "FINAL_VALIDATING"
    PR_CREATED = "PR_CREATED"
    IN_REVIEW = "IN_REVIEW"
    READY_FOR_HUMAN = "READY_FOR_HUMAN"
    COMPLETED = "COMPLETED"
    INVALID_SPEC = "INVALID_SPEC"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"


TERMINAL_STATUSES = frozenset(
    {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.INVALID_SPEC,
        ExecutionStatus.SCOPE_VIOLATION,
        ExecutionStatus.ESCALATED,
    }
)

# Only these edges are legal. Repair / review-fix edges are declared so later
# phases can use the machine; this module does not run those loops.
ALLOWED_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.PENDING: frozenset({ExecutionStatus.RUNNING, ExecutionStatus.INVALID_SPEC}),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.IMPLEMENTING,
            ExecutionStatus.INVALID_SPEC,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.IMPLEMENTING: frozenset(
        {
            ExecutionStatus.VALIDATING,
            ExecutionStatus.SCOPE_VIOLATION,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.VALIDATING: frozenset(
        {
            ExecutionStatus.TASK_COMPLETED,
            ExecutionStatus.IMPLEMENTING,
            ExecutionStatus.SCOPE_VIOLATION,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.TASK_COMPLETED: frozenset(
        {
            ExecutionStatus.IMPLEMENTING,
            ExecutionStatus.FINAL_VALIDATING,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.FINAL_VALIDATING: frozenset(
        {
            ExecutionStatus.PR_CREATED,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.PR_CREATED: frozenset(
        {
            ExecutionStatus.IN_REVIEW,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.IN_REVIEW: frozenset(
        {
            ExecutionStatus.READY_FOR_HUMAN,
            ExecutionStatus.IMPLEMENTING,
            ExecutionStatus.FAILED,
            ExecutionStatus.ESCALATED,
        }
    ),
    ExecutionStatus.READY_FOR_HUMAN: frozenset({ExecutionStatus.COMPLETED}),
    ExecutionStatus.COMPLETED: frozenset(),
    ExecutionStatus.INVALID_SPEC: frozenset(),
    ExecutionStatus.SCOPE_VIOLATION: frozenset(),
    ExecutionStatus.FAILED: frozenset({ExecutionStatus.RUNNING}),
    ExecutionStatus.ESCALATED: frozenset(),
}

JSON_FIELD_ORDER = (
    "schemaVersion",
    "taskId",
    "state",
    "currentTask",
    "completedTasks",
    "repairAttempts",
    "reviewAttempts",
    "lastValidation",
    "lastResult",
    "branch",
    "pullRequest",
)


@dataclass(frozen=True)
class ExecutionState:
    schema_version: int
    task_id: str
    state: ExecutionStatus
    current_task: str | None
    completed_tasks: tuple[str, ...]
    repair_attempts: int
    review_attempts: int
    last_validation: str | None
    last_result: str | None
    branch: str
    pull_request: str | int | None

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "schemaVersion": self.schema_version,
            "taskId": self.task_id,
            "state": self.state.value,
            "currentTask": self.current_task,
            "completedTasks": list(self.completed_tasks),
            "repairAttempts": self.repair_attempts,
            "reviewAttempts": self.review_attempts,
            "lastValidation": self.last_validation,
            "lastResult": self.last_result,
            "branch": self.branch,
            "pullRequest": self.pull_request,
        }
        return {key: payload[key] for key in JSON_FIELD_ORDER}


def load_execution_state_schema() -> dict[str, Any]:
    global _EXECUTION_STATE_SCHEMA
    if _EXECUTION_STATE_SCHEMA is None:
        _EXECUTION_STATE_SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return _EXECUTION_STATE_SCHEMA


def validate_execution_state_dict(instance: dict[str, Any]) -> None:
    try:
        jsonschema.validate(instance=instance, schema=load_execution_state_schema())
    except JsonSchemaValidationError as exc:
        path = ".".join(str(part) for part in exc.absolute_path) or "(root)"
        raise AgentError.invalid_input(f"invalid execution state: {path}: {exc.message}") from exc


def state_from_dict(instance: dict[str, Any]) -> ExecutionState:
    validate_execution_state_dict(instance)
    return ExecutionState(
        schema_version=int(instance["schemaVersion"]),
        task_id=instance["taskId"],
        state=ExecutionStatus(instance["state"]),
        current_task=instance["currentTask"],
        completed_tasks=tuple(instance["completedTasks"]),
        repair_attempts=int(instance["repairAttempts"]),
        review_attempts=int(instance["reviewAttempts"]),
        last_validation=instance["lastValidation"],
        last_result=instance["lastResult"],
        branch=instance["branch"],
        pull_request=instance["pullRequest"],
    )


def assert_allowed_transition(current: ExecutionStatus, target: ExecutionStatus) -> None:
    allowed = ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise AgentError.policy_violation(
            f"INVALID_TRANSITION: {current.value} -> {target.value}",
            code="INVALID_TRANSITION",
        )


def new_execution_state(spec: TaskSpec) -> ExecutionState:
    return ExecutionState(
        schema_version=1,
        task_id=spec.id,
        state=ExecutionStatus.PENDING,
        current_task=None,
        completed_tasks=(),
        repair_attempts=0,
        review_attempts=0,
        last_validation=None,
        last_result=None,
        branch=spec.target_branch,
        pull_request=None,
    )


def apply_transition(
    current: ExecutionState,
    target: ExecutionStatus | str,
    *,
    current_task: str | None | object = ...,
    completed_tasks: list[str] | tuple[str, ...] | object = ...,
    repair_attempts: int | object = ...,
    review_attempts: int | object = ...,
    last_validation: str | None | object = ...,
    last_result: str | None | object = ...,
    branch: str | object = ...,
    pull_request: str | int | None | object = ...,
) -> ExecutionState:
    if isinstance(target, ExecutionStatus):
        next_status = target
    else:
        try:
            next_status = ExecutionStatus(target)
        except ValueError as exc:
            raise AgentError.invalid_input(f"unknown execution state: {target}") from exc
    assert_allowed_transition(current.state, next_status)
    updated = replace(current, state=next_status)
    if current_task is not ...:
        updated = replace(updated, current_task=current_task)  # type: ignore[arg-type]
    if completed_tasks is not ...:
        updated = replace(updated, completed_tasks=tuple(completed_tasks))  # type: ignore[arg-type]
    if repair_attempts is not ...:
        updated = replace(updated, repair_attempts=int(repair_attempts))  # type: ignore[arg-type]
    if review_attempts is not ...:
        updated = replace(updated, review_attempts=int(review_attempts))  # type: ignore[arg-type]
    if last_validation is not ...:
        updated = replace(updated, last_validation=last_validation)  # type: ignore[arg-type]
    if last_result is not ...:
        updated = replace(updated, last_result=last_result)  # type: ignore[arg-type]
    if branch is not ...:
        updated = replace(updated, branch=str(branch))
    if pull_request is not ...:
        updated = replace(updated, pull_request=pull_request)  # type: ignore[arg-type]
    validate_execution_state_dict(updated.to_json_dict())
    return updated


def state_file_path(
    repo_root: Path | str,
    task_id: str,
    config: AgentConfig | None = None,
) -> Path:
    cfg = config or load_config()
    return Path(repo_root) / cfg.state.directory / f"{task_id}.json"


def read_state(path: Path | str) -> ExecutionState:
    state_path = Path(path)
    assert_current_state_regular_or_absent(state_path)
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentError.environment_failure(f"state file not found: {state_path}") from exc
    except OSError as exc:
        raise AgentError.environment_failure(f"state file could not be read: {state_path}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentError.invalid_input(f"state file is not valid JSON: {state_path}") from exc
    if not isinstance(payload, dict):
        raise AgentError.invalid_input("state file root must be an object")
    return state_from_dict(payload)


def write_state(path: Path | str, state: ExecutionState) -> None:
    payload = state.to_json_dict()
    validate_execution_state_dict(payload)
    state_path = Path(path)
    assert_current_state_regular_or_absent(state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    tmp_path.write_text(serialized, encoding="utf-8")
    tmp_path.replace(state_path)


def init_state(
    spec: TaskSpec,
    repo_root: Path | str,
    *,
    config: AgentConfig | None = None,
    overwrite: bool = False,
) -> ExecutionState:
    path = state_file_path(repo_root, spec.id, config=config)
    assert_current_state_regular_or_absent(path)
    if path.exists() and not overwrite:
        raise AgentError.policy_violation(
            f"execution state already exists: {path}",
            code="STATE_EXISTS",
        )
    state = new_execution_state(spec)
    write_state(path, state)
    return state
