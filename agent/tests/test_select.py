from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from agent.errors import AgentError
from agent.select import select_next_task
from agent.spec import parse_spec
from agent.state import new_execution_state

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SPEC = REPO_ROOT / "specs" / "tasks" / "example-task.md"


def test_selects_first_task_in_definition_order() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    selected = select_next_task(spec, new_execution_state(spec))
    assert selected is not None
    assert selected.id == "task-1"


def test_selects_next_task_after_completed() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    state = replace(new_execution_state(spec), completed_tasks=("task-1",))
    selected = select_next_task(spec, state)
    assert selected is not None
    assert selected.id == "task-2"


def test_all_completed_returns_none() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    state = replace(new_execution_state(spec), completed_tasks=("task-1", "task-2"))
    assert select_next_task(spec, state) is None


def test_dependency_blocked_current_task() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    state = replace(new_execution_state(spec), current_task="task-2")
    with pytest.raises(AgentError) as exc_info:
        select_next_task(spec, state)
    assert exc_info.value.code == "DEPENDENCY_BLOCKED"
    assert "task-1" in str(exc_info.value)
