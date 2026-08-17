from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from agent.errors import AgentError
from agent.spec import parse_spec
from agent.state import (
    ExecutionStatus,
    apply_transition,
    init_state,
    new_execution_state,
    read_state,
    write_state,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SPEC = REPO_ROOT / "specs" / "tasks" / "example-task.md"


def test_state_init_and_round_trip(tmp_path: Path) -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    created = init_state(spec, tmp_path)
    path = tmp_path / ".agent" / "state" / "phase2-step2.json"

    assert created.state is ExecutionStatus.PENDING
    assert created.task_id == "phase2-step2"
    assert created.branch == "feature/phase2-worker"
    assert created.current_task is None
    assert created.completed_tasks == ()
    assert path.is_file()
    assert read_state(path) == created


def test_state_read_write_preserves_fields(tmp_path: Path) -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    state = apply_transition(
        new_execution_state(spec),
        ExecutionStatus.RUNNING,
        current_task="task-1",
    )
    path = tmp_path / "state.json"
    write_state(path, state)
    loaded = read_state(path)
    assert loaded.current_task == "task-1"
    assert loaded.state is ExecutionStatus.RUNNING
    assert loaded.to_json_dict()["schemaVersion"] == 1


def test_init_state_refuses_overwrite(tmp_path: Path) -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    init_state(spec, tmp_path)
    with pytest.raises(AgentError) as exc_info:
        init_state(spec, tmp_path)
    assert exc_info.value.code == "STATE_EXISTS"


def test_valid_transition() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    pending = new_execution_state(spec)
    running = apply_transition(pending, ExecutionStatus.RUNNING)
    implementing = apply_transition(running, ExecutionStatus.IMPLEMENTING)
    validating = apply_transition(implementing, ExecutionStatus.VALIDATING)
    completed_task = apply_transition(
        validating,
        ExecutionStatus.TASK_COMPLETED,
        current_task="task-1",
        completed_tasks=["task-1"],
        last_result="PASSED",
    )
    assert completed_task.state is ExecutionStatus.TASK_COMPLETED
    assert completed_task.completed_tasks == ("task-1",)


def test_invalid_transition_is_rejected() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    pending = new_execution_state(spec)
    with pytest.raises(AgentError) as exc_info:
        apply_transition(pending, ExecutionStatus.COMPLETED)
    assert exc_info.value.code == "INVALID_TRANSITION"
    assert "PENDING -> COMPLETED" in str(exc_info.value)


def test_failed_can_retry() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    failed = replace(new_execution_state(spec), state=ExecutionStatus.FAILED)
    running = apply_transition(failed, ExecutionStatus.RUNNING)
    assert running.state is ExecutionStatus.RUNNING


def test_terminal_state_has_no_outbound() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    failed = replace(new_execution_state(spec), state=ExecutionStatus.ESCALATED)
    with pytest.raises(AgentError) as exc_info:
        apply_transition(failed, ExecutionStatus.RUNNING)
    assert exc_info.value.code == "INVALID_TRANSITION"
