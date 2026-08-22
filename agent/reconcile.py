"""Ephemeral execution control and durable GitHub PR reconciliation.

Execute uses prepare_execution_state. That path does not checkout a feature
branch, does not treat git history as Resume, and does not inspect GitHub PRs.

Deliver uses reconcile_open_pull. Durable sources are GitHub pull requests
identified by spec_id + head + base + work-unit marker.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent.config import AgentConfig
from agent.errors import AgentError
from agent.github_api import GitHubClient
from agent.pr import authorize_work_unit_reuse, is_same_work_unit_pull
from agent.spec import TaskSpec
from agent.state import (
    ExecutionState,
    ExecutionStatus,
    apply_transition,
    assert_current_state_regular_or_absent,
    new_execution_state,
    read_state,
    state_file_path,
    write_state,
)

IN_FLIGHT = frozenset(
    {
        ExecutionStatus.RUNNING,
        ExecutionStatus.IMPLEMENTING,
        ExecutionStatus.VALIDATING,
        ExecutionStatus.FINAL_VALIDATING,
    }
)


@dataclass(frozen=True)
class ReconcileResult:
    action: str
    state: ExecutionState
    reason: str

    @property
    def should_run_codex(self) -> bool:
        return self.action in {"continue", "retry"}


@dataclass(frozen=True)
class OpenPullReconcile:
    action: str
    pull: dict[str, Any] | None


def load_state_or_new(
    spec: TaskSpec,
    repo_root: Path | str,
    config: AgentConfig | None = None,
) -> ExecutionState:
    path = state_file_path(repo_root, spec.id, config=config)
    assert_current_state_regular_or_absent(path)
    if path.exists():
        return read_state(path)
    return new_execution_state(spec)


def prepare_execution_state(
    spec: TaskSpec,
    repo_root: Path | str,
    *,
    persist_state: bool = False,
    config: AgentConfig | None = None,
) -> ReconcileResult:
    """Load in-run Execution State. GitHub Actions uses persist_state=False."""
    root = Path(repo_root)
    if not persist_state:
        return ReconcileResult(
            "continue",
            new_execution_state(spec),
            "ephemeral start from the beginning",
        )
    path = state_file_path(root, spec.id, config=config)
    assert_current_state_regular_or_absent(path)
    if not path.exists():
        state = new_execution_state(spec)
        write_state(path, state)
        return ReconcileResult("continue", state, "initialized ephemeral execution state")
    state = read_state(path)
    result = _prepare_persisted_state(spec, state)
    write_state(path, result.state)
    return result


def reconcile_open_pull(spec: TaskSpec, github: GitHubClient) -> OpenPullReconcile:
    """Reuse a same-work-unit open PR or allow a new delivery.

    Same branch alone is not enough. spec_id, head.ref, base.ref, and the
    work-unit marker must all match.
    """
    pulls = github.list_open_pulls(head_branch=spec.target_branch)
    if len(pulls) > 1:
        raise AgentError.escalation_required(
            f"multiple open pull requests for {spec.target_branch}",
            code="UNSAFE_RECONCILE",
        )
    if not pulls:
        return OpenPullReconcile("create", None)
    pull = pulls[0]
    if not is_same_work_unit_pull(spec, pull):
        raise AgentError.escalation_required(
            "open pull request on the target branch is not the same work unit",
            code="WORK_UNIT_PR_MISMATCH",
        )
    authorize_work_unit_reuse(spec, pull)
    return OpenPullReconcile("reuse", pull)


def _prepare_persisted_state(spec: TaskSpec, state: ExecutionState) -> ReconcileResult:
    if state.task_id != spec.id:
        raise AgentError.escalation_required(
            f"state taskId {state.task_id} does not match spec {spec.id}",
            code="UNSAFE_RECONCILE",
        )
    if state.branch != spec.target_branch:
        raise AgentError.escalation_required(
            f"state branch {state.branch!r} does not match spec target_branch "
            f"{spec.target_branch!r}",
            code="STATE_BRANCH_MISMATCH",
        )
    if state.state is ExecutionStatus.ESCALATED:
        return ReconcileResult("block", state, "execution is ESCALATED; human action required")
    if state.state is ExecutionStatus.SCOPE_VIOLATION:
        return ReconcileResult("block", state, "SCOPE_VIOLATION is not auto-retried")
    if state.state is ExecutionStatus.INVALID_SPEC:
        return ReconcileResult("block", state, "INVALID_SPEC is not auto-retried")
    if state.state is ExecutionStatus.COMPLETED:
        return ReconcileResult("block", state, "COMPLETED is not auto-retried")
    if state.state is ExecutionStatus.PR_CREATED:
        return ReconcileResult(
            "continue",
            new_execution_state(spec),
            "ephemeral start; existing PR reuse is deliver-side",
        )
    if state.state is ExecutionStatus.FAILED:
        return ReconcileResult(
            "retry",
            apply_transition(state, ExecutionStatus.RUNNING),
            "retrying FAILED work unit",
        )
    if state.state in IN_FLIGHT:
        return ReconcileResult(
            "continue",
            _recover_in_flight(state),
            "recovered interrupted execution",
        )
    return ReconcileResult("continue", state, "ephemeral execution state is consistent")


def _recover_in_flight(state: ExecutionState) -> ExecutionState:
    """Crash recovery for local persist_state=True. Not cross-run GHA Resume.

    In-flight statuses have no legal edge back to a checkpoint, so this rewrite
    is local execution control rather than a state-machine transition.
    """
    if state.state is ExecutionStatus.FINAL_VALIDATING:
        return state
    if state.completed_tasks:
        return replace(state, state=ExecutionStatus.TASK_COMPLETED)
    return replace(
        state,
        state=ExecutionStatus.PENDING,
        current_task=None,
        repair_attempts=0,
    )
