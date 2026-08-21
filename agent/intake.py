"""GitHub Actions intake helpers: spec parse, loop prevention, history, ephemeral state guard.

This module does not commit, push, create branches, or open pull requests.
Execution State guard is local ephemeral control, not GHA Resume.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent.config import AgentConfig, load_config
from agent.errors import AgentError, ErrorCategory
from agent.gitutil import normalize_git_path, require_git_ok, run_git
from agent.scope import validate_spec_scope_policy
from agent.spec import TaskSpec, parse_spec
from agent.state import ExecutionStatus, read_state, state_file_path

NULL_SHA = "0" * 40
STARTABLE_STATUSES = frozenset(
    {
        ExecutionStatus.PENDING,
        ExecutionStatus.TASK_COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.PR_CREATED,
        ExecutionStatus.FINAL_VALIDATING,
        ExecutionStatus.RUNNING,
        ExecutionStatus.IMPLEMENTING,
        ExecutionStatus.VALIDATING,
    }
)
IN_FLIGHT_STATUSES = frozenset(
    {
        ExecutionStatus.RUNNING,
        ExecutionStatus.IMPLEMENTING,
        ExecutionStatus.VALIDATING,
        ExecutionStatus.FINAL_VALIDATING,
    }
)


@dataclass(frozen=True)
class IntakeResult:
    valid: bool
    should_execute: bool
    task_id: str
    spec_path: str
    base_branch: str
    target_branch: str
    reason: str

    def to_output_map(self) -> dict[str, str]:
        return {
            "valid": "true" if self.valid else "false",
            "should_execute": "true" if self.should_execute else "false",
            "task_id": self.task_id,
            "spec_path": self.spec_path,
            "base_branch": self.base_branch,
            "target_branch": self.target_branch,
            "reason": self.reason,
        }

    def to_json_dict(self) -> dict[str, object]:
        return {
            "ok": self.valid,
            "valid": self.valid,
            "should_execute": self.should_execute,
            "task_id": self.task_id,
            "spec_path": self.spec_path,
            "base_branch": self.base_branch,
            "target_branch": self.target_branch,
            "reason": self.reason,
        }


def is_null_sha(sha: str | None) -> bool:
    if sha is None or not sha.strip():
        return True
    stripped = sha.strip()
    return set(stripped) <= {"0"}


def write_github_output(path: Path | str, values: dict[str, str]) -> None:
    """Append `name=value` lines as documented for `$GITHUB_OUTPUT`.

    Values are flattened to a single line so Invalid Spec reasons (YAML errors)
    can still be written without switching to the multiline delimiter form.
    """
    output_path = Path(path)
    lines: list[str] = []
    for key, value in values.items():
        if not key or any(ch in key for ch in "=\r\n"):
            raise AgentError.invalid_input(f"invalid GitHub output name: {key!r}")
        flattened = " ".join(value.splitlines()).strip()
        lines.append(f"{key}={flattened}")
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def evaluate_intake(
    *,
    repo_root: Path | str,
    event_name: str,
    ref_name: str,
    sha: str,
    before_sha: str | None = None,
    spec_path: str | None = None,
    config: AgentConfig | None = None,
) -> IntakeResult:
    cfg = config or load_config()
    root = Path(repo_root)
    spec_dir = cfg.task_spec.directory.replace("\\", "/").strip("/")

    try:
        selected = _select_spec_path(
            root,
            event_name=event_name,
            sha=sha,
            before_sha=before_sha,
            spec_path=spec_path,
            spec_dir=spec_dir,
        )
    except AgentError as exc:
        if exc.category is ErrorCategory.ENVIRONMENT_FAILURE:
            raise
        return _invalid(reason=str(exc), spec_path=_safe_rel(spec_path))

    try:
        spec = parse_spec(root / selected)
    except AgentError as exc:
        return _invalid(reason=str(exc), spec_path=selected)

    if ref_name != spec.base_branch:
        return IntakeResult(
            valid=True,
            should_execute=False,
            task_id=spec.id,
            spec_path=selected,
            base_branch=spec.base_branch,
            target_branch=spec.target_branch,
            reason=(
                f"ref {ref_name!r} is not spec base_branch {spec.base_branch!r}; "
                "feature-branch pushes are not task intake"
            ),
        )
    return IntakeResult(
        valid=True,
        should_execute=True,
        task_id=spec.id,
        spec_path=selected,
        base_branch=spec.base_branch,
        target_branch=spec.target_branch,
        reason="ok",
    )


def prepare_execute(
    spec: TaskSpec | Path | str,
    *,
    repo_root: Path | str,
    config: AgentConfig | None = None,
) -> TaskSpec:
    cfg = config or load_config()
    root = Path(repo_root)
    parsed = spec if isinstance(spec, TaskSpec) else parse_spec(_spec_file(repo_root, spec))
    validate_spec_scope_policy(parsed, cfg.runtime_edit_policy)
    assert_required_history(root, base_branch=parsed.base_branch)
    assert_execution_guard(parsed, root, config=cfg)
    return parsed


def assert_required_history(repo_root: Path | str, *, base_branch: str | None = None) -> None:
    """Fail closed unless commit objects needed for later comparison exist."""
    sha = require_git_ok(run_git(repo_root, "rev-parse", "HEAD"), "rev-parse", "HEAD").strip()
    require_git_ok(
        run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}"),
        "cat-file",
    )
    if not base_branch:
        return
    for ref in (f"origin/{base_branch}", base_branch):
        probed = run_git(repo_root, "rev-parse", "--verify", ref)
        if probed.returncode != 0:
            continue
        require_git_ok(
            run_git(repo_root, "merge-base", sha, ref),
            "merge-base",
        )
        return
    raise AgentError.environment_failure(
        f"base branch ref not available locally: {base_branch}",
        code="MISSING_BASE_REF",
    )


def assert_execution_guard(
    spec: TaskSpec,
    repo_root: Path | str,
    *,
    config: AgentConfig | None = None,
) -> None:
    reason = execution_guard_reason(spec, repo_root, config=config)
    if reason is not None:
        raise AgentError.policy_violation(reason, code="EXECUTION_GUARD")


def execution_guard_reason(
    spec: TaskSpec,
    repo_root: Path | str,
    *,
    config: AgentConfig | None = None,
) -> str | None:
    """Block local leftover terminal states. Missing state always allows start.

    This is ephemeral execution control. It does not inspect GitHub PRs or git
    history, and GitHub Actions re-runs do not use the file as Resume state.
    """
    path = state_file_path(repo_root, spec.id, config=config)
    if not path.exists():
        return None
    state = read_state(path)
    if state.state in STARTABLE_STATUSES:
        return None
    if state.state in IN_FLIGHT_STATUSES:
        return f"execution already in progress: {state.state.value}"
    return f"execution state {state.state.value} is not startable in this phase"


def _select_spec_path(
    repo_root: Path,
    *,
    event_name: str,
    sha: str,
    before_sha: str | None,
    spec_path: str | None,
    spec_dir: str,
) -> str:
    if event_name == "workflow_dispatch":
        if not spec_path or not spec_path.strip():
            raise AgentError.invalid_input("workflow_dispatch requires spec_path")
        return _resolve_spec_rel(repo_root, spec_path, spec_dir)
    if event_name != "push":
        raise AgentError.policy_violation(
            f"unsupported intake event: {event_name}",
            code="UNSUPPORTED_EVENT",
        )
    changed = list_changed_spec_paths(
        repo_root,
        before_sha=before_sha,
        sha=sha,
        spec_dir=spec_dir,
    )
    if not changed:
        raise AgentError.invalid_input("push did not change any Task Spec markdown")
    if len(changed) > 1:
        raise AgentError.invalid_input(
            "push changed multiple Task Specs; intake is one spec per run: " + ", ".join(changed)
        )
    return changed[0]


def list_changed_spec_paths(
    repo_root: Path | str,
    *,
    before_sha: str | None,
    sha: str,
    spec_dir: str,
) -> tuple[str, ...]:
    if not sha or is_null_sha(sha):
        raise AgentError.environment_failure("GITHUB_SHA is missing", code="MISSING_SHA")
    require_git_ok(run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}"), "cat-file")
    if is_null_sha(before_sha):
        output = require_git_ok(
            run_git(
                repo_root,
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                sha,
            ),
            "diff-tree",
        )
    else:
        before = (before_sha or "").strip()
        require_git_ok(
            run_git(repo_root, "cat-file", "-e", f"{before}^{{commit}}"),
            "cat-file",
        )
        output = require_git_ok(
            run_git(
                repo_root,
                "diff",
                "--name-only",
                "--diff-filter=AMCR",
                before,
                sha,
            ),
            "diff",
        )
    prefix = spec_dir.replace("\\", "/").rstrip("/") + "/"
    paths: list[str] = []
    for raw in output.splitlines():
        path = normalize_git_path(raw)
        if path.startswith(prefix) and path.endswith(".md"):
            paths.append(path)
    return tuple(sorted(set(paths)))


def _resolve_spec_rel(repo_root: Path, spec_path: str, spec_dir: str) -> str:
    root = repo_root.resolve()
    candidate = Path(spec_path)
    path = candidate.resolve() if candidate.is_absolute() else (root / spec_path).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise AgentError.policy_violation(
            f"spec path escapes repository: {spec_path}",
            code="SPEC_PATH_ESCAPE",
        ) from exc
    prefix = spec_dir.replace("\\", "/").rstrip("/") + "/"
    if not relative.startswith(prefix) or not relative.endswith(".md"):
        raise AgentError.policy_violation(
            f"spec path must be markdown under {prefix}: {relative}",
            code="SPEC_PATH_OUT_OF_DIR",
        )
    if not path.is_file():
        raise AgentError.invalid_input(f"spec file not found: {relative}")
    return relative


def _invalid(
    *,
    reason: str,
    spec_path: str = "",
    task_id: str = "",
    base_branch: str = "",
    target_branch: str = "",
) -> IntakeResult:
    return IntakeResult(
        valid=False,
        should_execute=False,
        task_id=task_id,
        spec_path=spec_path,
        base_branch=base_branch,
        target_branch=target_branch,
        reason=reason,
    )


def _safe_rel(spec_path: str | None) -> str:
    if spec_path is None:
        return ""
    return spec_path.replace("\\", "/")


def _spec_file(repo_root: Path | str, spec: Path | str) -> Path:
    path = Path(spec)
    if path.is_absolute():
        return path
    return Path(repo_root) / path
