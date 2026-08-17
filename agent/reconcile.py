"""GitHub Reconciliation for delivery; local State is optional.

GHA re-runs start from PENDING when `.agent/state` is absent (ephemeral).
Durable sources are Git branch / history / Pull Request. Unsafe mismatch → ESCALATED.
If a local state file exists (dev / same workspace), in-flight statuses are
rewritten to the last safe checkpoint because those edges are not legal
state-machine transitions. That is not cross-run resume.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent.errors import AgentError
from agent.github_api import GitHubClient
from agent.gitutil import capture_snapshot
from agent.gitwrite import (
    branch_exists_locally,
    commits_ahead_of,
    prepare_feature_worktree,
    remote_branch_sha,
)
from agent.spec import TaskSpec
from agent.state import (
    ExecutionState,
    ExecutionStatus,
    apply_transition,
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
    pull: dict[str, Any] | None = None

    @property
    def should_run_codex(self) -> bool:
        return self.action in {"continue", "retry"}

    @property
    def already_delivered(self) -> bool:
        return self.action == "skip"


def load_state_or_new(spec: TaskSpec, repo_root: Path | str) -> ExecutionState:
    path = state_file_path(repo_root, spec.id)
    if path.exists():
        return read_state(path)
    return new_execution_state(spec)


def reconcile_work_unit(
    spec: TaskSpec,
    repo_root: Path | str,
    *,
    github: GitHubClient | None = None,
    persist_state: bool = True,
) -> ReconcileResult:
    root = Path(repo_root)
    prepare_feature_worktree(root, spec.target_branch)
    state = load_state_or_new(spec, root)
    result = _reconcile(spec, root, state, github)
    if persist_state:
        write_state(state_file_path(root, spec.id), result.state)
    return result


def _reconcile(
    spec: TaskSpec,
    root: Path,
    state: ExecutionState,
    github: GitHubClient | None,
) -> ReconcileResult:
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

    local_branch = branch_exists_locally(root, spec.target_branch)
    remote_sha = remote_branch_sha(root, spec.target_branch)
    branch_present = local_branch or remote_sha is not None
    ahead: tuple[str, ...] = ()
    if branch_present:
        head_ref = spec.target_branch if local_branch else f"origin/{spec.target_branch}"
        ahead = commits_ahead_of(root, f"origin/{spec.base_branch}", head_ref)
        if not ahead:
            ahead = commits_ahead_of(root, spec.base_branch, head_ref)

    pull = _unique_open_pull(github, spec.target_branch)

    if state.state is ExecutionStatus.ESCALATED:
        return ReconcileResult(
            "block", state, "execution is ESCALATED; human action required", pull
        )
    if state.state is ExecutionStatus.SCOPE_VIOLATION:
        return ReconcileResult("block", state, "SCOPE_VIOLATION is not auto-retried", pull)
    if state.state is ExecutionStatus.INVALID_SPEC:
        return ReconcileResult("block", state, "INVALID_SPEC is not auto-retried", pull)

    if pull is not None:
        return _reconcile_existing_pull(state, pull)

    if state.state is ExecutionStatus.PR_CREATED and github is not None:
        raise AgentError.escalation_required(
            "state is PR_CREATED but no open pull request exists for the feature branch",
            code="STATE_PR_MISMATCH",
        )

    if state.state is ExecutionStatus.FAILED:
        return ReconcileResult(
            "retry",
            apply_transition(state, ExecutionStatus.RUNNING),
            "retrying FAILED work unit",
            pull,
        )

    if state.state in IN_FLIGHT:
        recovered = _recover_in_flight(state)
        return ReconcileResult("continue", recovered, "recovered interrupted execution", pull)

    if state.completed_tasks and not branch_present and not ahead:
        snapshot = capture_snapshot(root)
        if not snapshot.dirty:
            raise AgentError.escalation_required(
                "state lists completed tasks but the feature branch has no corresponding commits",
                code="STATE_COMMIT_MISMATCH",
            )

    if state.state is ExecutionStatus.PENDING and not state.completed_tasks and ahead:
        raise AgentError.escalation_required(
            "feature branch has commits but execution state is still PENDING",
            code="STATE_GIT_MISMATCH",
        )

    return ReconcileResult("continue", state, "state is consistent with git", pull)


def _unique_open_pull(github: GitHubClient | None, branch: str) -> dict[str, Any] | None:
    if github is None:
        return None
    pulls = github.list_open_pulls(head_branch=branch)
    if len(pulls) > 1:
        raise AgentError.escalation_required(
            f"multiple open pull requests for {branch}",
            code="UNSAFE_RECONCILE",
        )
    return pulls[0] if pulls else None


def _reconcile_existing_pull(state: ExecutionState, pull: dict[str, Any]) -> ReconcileResult:
    number = pull.get("number")
    if state.state is ExecutionStatus.PR_CREATED:
        if state.pull_request not in {None, "", number, str(number)}:
            raise AgentError.escalation_required(
                f"state pullRequest {state.pull_request!r} does not match open PR {number!r}",
                code="STATE_PR_MISMATCH",
            )
        updated = (
            state
            if state.pull_request in {number, str(number)}
            else replace(
                state, pull_request=number if isinstance(number, int) else state.pull_request
            )
        )
        return ReconcileResult("skip", updated, "pull request already exists", pull)
    if state.state is ExecutionStatus.FINAL_VALIDATING:
        adopted = apply_transition(state, ExecutionStatus.PR_CREATED, pull_request=number)
        return ReconcileResult("skip", adopted, "adopted existing pull request", pull)
    raise AgentError.escalation_required(
        "open pull request exists but execution state is not PR_CREATED",
        code="STATE_PR_MISMATCH",
    )


def _recover_in_flight(state: ExecutionState) -> ExecutionState:
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
