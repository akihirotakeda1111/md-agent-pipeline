"""Orchestrator Git write operations. Codex never calls this module.

Forbidden: force push, history rewrite, commit --amend, rebase.
Git subprocess env is sanitized and never receives CODEX_API_KEY, GITHUB_TOKEN,
or AGENT_PR_PAT.
Checkout uses persist-credentials: false. Push injects HTTPS auth via a
process-local GIT_CONFIG extraHeader so Final Verification cannot reuse it.
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

from agent.codex_runner import build_allowlisted_env
from agent.errors import AgentError
from agent.gitutil import (
    change_path_list,
    collect_changes,
    require_git_ok,
    run_git,
)

# Official github-actions bot identity used with GITHUB_TOKEN commits.
# https://github.com/actions/checkout#push-a-commit-using-the-built-in-token
GITHUB_ACTIONS_BOT_NAME = "github-actions[bot]"
GITHUB_ACTIONS_BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

GIT_IDENTITY_ENV = frozenset(
    {
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_INDEX_FILE",
    }
)


def run_git_write(
    repo_root: Path | str, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    _assert_safe_git_args(args)
    env = build_allowlisted_env(extra_allow=GIT_IDENTITY_ENV)
    if extra_env:
        env.update(extra_env)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentError.environment_failure(
            "git executable not found", code="GIT_NOT_FOUND"
        ) from exc


def configure_committer(repo_root: Path | str) -> None:
    require_git_ok(
        run_git_write(repo_root, "config", "user.name", GITHUB_ACTIONS_BOT_NAME),
        "config",
        "user.name",
    )
    require_git_ok(
        run_git_write(repo_root, "config", "user.email", GITHUB_ACTIONS_BOT_EMAIL),
        "config",
        "user.email",
    )


def branch_exists_locally(repo_root: Path | str, branch: str) -> bool:
    completed = run_git(repo_root, "rev-parse", "--verify", branch)
    return completed.returncode == 0


def remote_branch_sha(repo_root: Path | str, branch: str) -> str | None:
    completed = run_git(repo_root, "rev-parse", "--verify", f"origin/{branch}")
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def fetch_remote_branch(repo_root: Path | str, branch: str) -> bool:
    remotes = require_git_ok(run_git(repo_root, "remote"), "remote")
    if "origin" not in remotes.split():
        return False
    completed = run_git_write(repo_root, "fetch", "origin", branch)
    return completed.returncode == 0


def checkout_existing_branch(repo_root: Path | str, branch: str) -> None:
    if fetch_remote_branch(repo_root, branch) and remote_branch_sha(repo_root, branch):
        require_git_ok(
            run_git_write(repo_root, "checkout", "-B", branch, f"origin/{branch}"),
            "checkout",
            "-B",
            branch,
        )
        return
    if branch_exists_locally(repo_root, branch):
        require_git_ok(
            run_git_write(repo_root, "checkout", branch),
            "checkout",
            branch,
        )
        return
    raise AgentError.environment_failure(
        f"feature branch not available: {branch}",
        code="GIT_FAILED",
    )


def create_branch_from_sha(repo_root: Path | str, branch: str, sha: str) -> None:
    require_git_ok(
        run_git_write(repo_root, "checkout", "-B", branch, sha),
        "checkout",
        "-B",
        branch,
    )


def checkout_delivery_parent(repo_root: Path | str, branch: str, base_sha: str) -> None:
    """Checkout the feature branch only when its tip equals ``base_sha``.

    Missing branch is created from ``base_sha``. Divergence is fail-closed.
    Rebase is forbidden.
    """
    if not commit_exists(repo_root, base_sha):
        raise AgentError.escalation_required(
            f"report base_sha {base_sha} is not a commit in this repository",
            code="BASE_SHA_MISSING",
        )
    fetch_remote_branch(repo_root, branch)
    remote = remote_branch_sha(repo_root, branch)
    if remote is not None:
        if remote != base_sha:
            raise AgentError.escalation_required(
                f"feature branch {branch} is at {remote}, expected {base_sha}",
                code="BASE_SHA_MISMATCH",
            )
        checkout_existing_branch(repo_root, branch)
    elif branch_exists_locally(repo_root, branch):
        checkout_existing_branch(repo_root, branch)
        actual = head_sha(repo_root)
        if actual != base_sha:
            raise AgentError.escalation_required(
                f"local branch {branch} is at {actual}, expected {base_sha}",
                code="BASE_SHA_MISMATCH",
            )
    else:
        create_branch_from_sha(repo_root, branch, base_sha)
    actual = head_sha(repo_root)
    if actual != base_sha:
        raise AgentError.escalation_required(
            f"HEAD {actual} does not match report base_sha {base_sha}",
            code="BASE_SHA_MISMATCH",
        )


def prepare_feature_worktree(repo_root: Path | str, branch: str) -> bool:
    """Checkout the feature branch when it already exists. Return True if checked out."""
    if fetch_remote_branch(repo_root, branch) or branch_exists_locally(repo_root, branch):
        if remote_branch_sha(repo_root, branch) or branch_exists_locally(repo_root, branch):
            checkout_existing_branch(repo_root, branch)
            return True
    return False


def commits_ahead_of(repo_root: Path | str, base_ref: str, head_ref: str) -> tuple[str, ...]:
    completed = run_git(
        repo_root,
        "rev-list",
        "--count",
        f"{base_ref}..{head_ref}",
    )
    if completed.returncode != 0:
        return ()
    listed = require_git_ok(
        run_git(repo_root, "rev-list", "--reverse", f"{base_ref}..{head_ref}"),
        "rev-list",
    )
    return tuple(line.strip() for line in listed.splitlines() if line.strip())


def commit_exists(repo_root: Path | str, sha: str) -> bool:
    completed = run_git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}")
    return completed.returncode == 0


def export_patch(repo_root: Path | str, base_sha: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    paths = list(change_path_list(collect_changes(repo_root, base_sha)))
    if not paths:
        dest.write_text("", encoding="utf-8")
        return dest
    git_dir = require_git_ok(run_git(repo_root, "rev-parse", "--git-dir"), "rev-parse").strip()
    git_dir_path = Path(git_dir) if Path(git_dir).is_absolute() else Path(repo_root) / git_dir
    index_src = git_dir_path / "index"
    tmp_index = dest.with_name(dest.name + ".index")
    if index_src.is_file():
        tmp_index.write_bytes(index_src.read_bytes())
    else:
        tmp_index.write_bytes(b"")
    extra = {"GIT_INDEX_FILE": str(tmp_index)}
    require_git_ok(
        run_git_write(repo_root, "add", "-f", "--", *paths, extra_env=extra),
        "add",
    )
    patch = require_git_ok(
        run_git_write(repo_root, "diff", "--cached", "--binary", extra_env=extra),
        "diff",
        "--cached",
    )
    dest.write_text(patch, encoding="utf-8")
    tmp_index.unlink(missing_ok=True)
    return dest


def apply_patch(repo_root: Path | str, patch_path: Path) -> None:
    if not patch_path.is_file():
        raise AgentError.environment_failure(
            f"patch file not found: {patch_path}",
            code="PATCH_APPLY_FAILED",
        )
    text = patch_path.read_text(encoding="utf-8")
    if not text.strip():
        return
    completed = run_git_write(repo_root, "apply", "--whitespace=nowarn", str(patch_path))
    if completed.returncode != 0:
        raise AgentError.escalation_required(
            f"failed to apply patch: {(completed.stderr or completed.stdout).strip()}",
            code="PATCH_APPLY_FAILED",
        )
    paths = list(change_path_list(collect_changes(repo_root, head_sha(repo_root))))
    if paths:
        require_git_ok(
            run_git_write(repo_root, "add", "-f", "--", *paths),
            "add",
        )


def commit_paths(
    repo_root: Path | str,
    paths: list[str],
    message: str,
    *,
    allow_empty: bool = False,
    force_add: bool = False,
) -> str | None:
    if not paths and not allow_empty:
        return None
    configure_committer(repo_root)
    if paths:
        add_args = ["add"]
        if force_add:
            add_args.append("-f")
        add_args.extend(["--", *paths])
        require_git_ok(run_git_write(repo_root, *add_args), "add")
    commit_args = ["commit", "-m", message]
    if allow_empty:
        commit_args.append("--allow-empty")
    completed = run_git_write(repo_root, *commit_args)
    if completed.returncode != 0:
        combined = f"{completed.stdout}\n{completed.stderr}".lower()
        if "nothing to commit" in combined:
            return None
        raise AgentError.environment_failure(
            f"git commit failed: {(completed.stderr or completed.stdout).strip()}",
            code="GIT_FAILED",
        )
    return require_git_ok(run_git(repo_root, "rev-parse", "HEAD"), "rev-parse", "HEAD").strip()


def _push_auth_env() -> dict[str, str]:
    """HTTPS auth for the git push subprocess only. Not written to .git/config.

    Uses GITHUB_TOKEN, never AGENT_PR_PAT. The PAT is reserved for create_pull.
    """
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        return {}
    server = (os.environ.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    basic = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{server}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def push_branch(repo_root: Path | str, branch: str) -> None:
    completed = run_git_write(
        repo_root,
        "push",
        "origin",
        f"HEAD:refs/heads/{branch}",
        extra_env=_push_auth_env(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        lowered = detail.lower()
        if "non-fast-forward" in lowered or "failed to push some refs" in lowered:
            raise AgentError.escalation_required(
                f"push would rewrite or diverge history: {detail}",
                code="NON_FAST_FORWARD",
            )
        raise AgentError.environment_failure(
            f"git push failed: {detail}",
            code="GIT_PUSH_FAILED",
        )


def current_branch(repo_root: Path | str) -> str:
    return require_git_ok(
        run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD"),
        "rev-parse",
    ).strip()


def head_sha(repo_root: Path | str) -> str:
    return require_git_ok(run_git(repo_root, "rev-parse", "HEAD"), "rev-parse", "HEAD").strip()


def _assert_safe_git_args(args: tuple[str, ...]) -> None:
    joined = " ".join(args)
    if "--force-with-lease" in args or "-f" in args[1:]:
        if args[:1] != ("checkout",) and args[:1] != ("add",):
            raise AgentError.policy_violation(
                f"force git argument is forbidden: {joined}",
                code="FORBIDDEN_GIT",
            )
    if "--force" in args and args[:1] not in {("checkout",), ("add",)}:
        raise AgentError.policy_violation(
            f"force git argument is forbidden: {joined}",
            code="FORBIDDEN_GIT",
        )
    if args[:1] == ("push",) and any(
        item in {"--force", "-f", "--force-with-lease"} for item in args
    ):
        raise AgentError.policy_violation("force push is forbidden", code="FORBIDDEN_GIT")
    if args[:1] == ("commit",) and "--amend" in args:
        raise AgentError.policy_violation("commit amend is forbidden", code="FORBIDDEN_GIT")
    if args[:1] == ("rebase",) or "rebase" in args:
        raise AgentError.policy_violation("rebase is forbidden", code="FORBIDDEN_GIT")
    if args[:1] == ("filter-branch",):
        raise AgentError.policy_violation("history rewrite is forbidden", code="FORBIDDEN_GIT")
