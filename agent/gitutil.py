"""Read-only Git inspection for BASE_SHA and working-tree diffs.

This module does not commit, push, create branches, or rewrite history.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.errors import AgentError


@dataclass(frozen=True)
class GitSnapshot:
    base_sha: str
    dirty: bool
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True)
class GitChange:
    path: str
    status: str
    old_path: str | None = None

    @property
    def paths(self) -> tuple[str, ...]:
        if self.old_path:
            return (self.old_path, self.path)
        return (self.path,)


def run_git(repo_root: Path | str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentError.environment_failure(
            "git executable not found", code="GIT_NOT_FOUND"
        ) from exc


def require_git_ok(completed: subprocess.CompletedProcess[str], *args: str) -> str:
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise AgentError.environment_failure(
            f"git {' '.join(args)} failed: {detail}",
            code="GIT_FAILED",
        )
    return completed.stdout


def capture_snapshot(repo_root: Path | str) -> GitSnapshot:
    sha = require_git_ok(run_git(repo_root, "rev-parse", "HEAD"), "rev-parse", "HEAD").strip()
    porcelain = require_git_ok(
        run_git(repo_root, "status", "--porcelain=v1", "-uall"),
        "status",
    )
    dirty_paths = tuple(
        path for path in _porcelain_paths(porcelain) if not _is_orchestrator_state_path(path)
    )
    return GitSnapshot(base_sha=sha, dirty=bool(dirty_paths), dirty_paths=dirty_paths)


def assert_clean_worktree(snapshot: GitSnapshot) -> None:
    if snapshot.dirty:
        preview = ", ".join(snapshot.dirty_paths[:8])
        raise AgentError.policy_violation(
            "working tree is dirty; cannot isolate Codex changes from existing "
            f"uncommitted files: {preview}",
            code="DIRTY_WORKTREE",
        )


def collect_changes(repo_root: Path | str, base_sha: str) -> tuple[GitChange, ...]:
    """Collect tracked diffs vs BASE_SHA plus untracked files.

    ``git diff --name-only`` alone is not enough: untracked files are included.
    """
    by_path: dict[str, GitChange] = {}
    for change in _parse_name_status(
        require_git_ok(
            run_git(repo_root, "diff", "--name-status", "--find-renames", base_sha),
            "diff",
        )
    ):
        by_path[change.path] = change
    for change in _parse_name_status(
        require_git_ok(
            run_git(repo_root, "diff", "--name-status", "--cached", "--find-renames"),
            "diff",
            "--cached",
        )
    ):
        by_path[change.path] = change
    untracked = require_git_ok(
        run_git(repo_root, "ls-files", "--others", "--exclude-standard"),
        "ls-files",
    )
    for raw in untracked.splitlines():
        path = normalize_git_path(raw)
        if path:
            by_path[path] = GitChange(path=path, status="untracked")
    return tuple(by_path.values())


def working_tree_diff_text(repo_root: Path | str, base_sha: str, *, limit: int = 8000) -> str:
    tracked = require_git_ok(run_git(repo_root, "diff", "--find-renames", base_sha), "diff")
    untracked = [
        change.path
        for change in collect_changes(repo_root, base_sha)
        if change.status == "untracked"
    ]
    parts = [tracked.strip()]
    if untracked:
        parts.append("Untracked files:\n" + "\n".join(f"- {path}" for path in untracked))
    text = "\n\n".join(part for part in parts if part)
    if len(text) > limit:
        return text[:limit] + "\n...[truncated]..."
    return text


def _is_orchestrator_state_path(path: str) -> bool:
    normalized = normalize_git_path(path)
    return normalized == ".agent/state" or normalized.startswith(".agent/state/")


def normalize_git_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _porcelain_paths(porcelain: str) -> list[str]:
    paths: list[str] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        body = line[3:]
        if " -> " in body:
            old, new = body.split(" -> ", 1)
            paths.extend([normalize_git_path(old), normalize_git_path(new)])
        else:
            paths.append(normalize_git_path(body))
    return paths


def _parse_name_status(output: str) -> list[GitChange]:
    changes: list[GitChange] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")) and len(parts) >= 3:
            changes.append(
                GitChange(
                    path=normalize_git_path(parts[2]),
                    status="renamed" if status.startswith("R") else "copied",
                    old_path=normalize_git_path(parts[1]),
                )
            )
            continue
        if len(parts) < 2:
            continue
        path = normalize_git_path(parts[1])
        kind = {
            "A": "added",
            "M": "modified",
            "T": "modified",
            "D": "deleted",
        }.get(status[:1], status.lower())
        changes.append(GitChange(path=path, status=kind))
    return changes
