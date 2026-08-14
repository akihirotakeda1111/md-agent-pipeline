"""Deterministic next-task selection. No LLM involved."""

from __future__ import annotations

from agent.errors import AgentError
from agent.spec import SpecTask, TaskSpec
from agent.state import ExecutionState


def missing_dependencies(task: SpecTask, completed: set[str]) -> list[str]:
    return [dep for dep in task.depends_on if dep not in completed]


def select_next_task(spec: TaskSpec, state: ExecutionState) -> SpecTask | None:
    """Return the next incomplete task in definition order whose deps are done.

    If `state.current_task` is set and not completed, that task is returned
    only when its dependencies are satisfied. Otherwise fail closed.
    """
    known = {task.id: task for task in spec.tasks}
    completed = set(state.completed_tasks)
    unknown_completed = completed.difference(known)
    if unknown_completed:
        unknown_id = sorted(unknown_completed)[0]
        raise AgentError.invalid_input(
            f"completedTasks contains unknown id: {unknown_id}",
            code="INVALID_STATE",
        )

    if state.current_task is not None and state.current_task not in completed:
        current = known.get(state.current_task)
        if current is None:
            raise AgentError.invalid_input(
                f"currentTask is not in spec: {state.current_task}",
                code="INVALID_STATE",
            )
        missing = missing_dependencies(current, completed)
        if missing:
            raise AgentError.policy_violation(
                f"DEPENDENCY_BLOCKED: {current.id} waiting for {', '.join(missing)}",
                code="DEPENDENCY_BLOCKED",
            )
        return current

    blocked: list[tuple[SpecTask, list[str]]] = []
    for task in spec.tasks:
        if task.id in completed:
            continue
        missing = missing_dependencies(task, completed)
        if missing:
            blocked.append((task, missing))
            continue
        return task

    if blocked:
        task, missing = blocked[0]
        raise AgentError.policy_violation(
            f"DEPENDENCY_BLOCKED: {task.id} waiting for {', '.join(missing)}",
            code="DEPENDENCY_BLOCKED",
        )
    return None
