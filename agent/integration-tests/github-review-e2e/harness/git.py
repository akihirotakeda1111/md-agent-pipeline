from __future__ import annotations

from pathlib import Path

from .process import run


class GitRepository:
    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def clone(cls, repo: str, root: Path) -> GitRepository:
        run(["gh", "repo", "clone", repo, str(root), "--", "--filter=blob:none"])
        result = cls(root)
        result.git("config", "user.name", "Phase 7 E2E")
        result.git("config", "user.email", "github-review-e2e@example.invalid")
        return result

    def git(self, *args: str, check: bool = True):
        return run(["git", *args], cwd=self.root, check=check)

    def checkout_base(self, branch: str) -> None:
        self.git("fetch", "origin", branch)
        self.git("checkout", "-B", branch, f"origin/{branch}")

    def create_source_branch(self, branch: str) -> None:
        self.git("checkout", "-b", branch)

    def write(self, relative: str, content: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    def commit_spec(self, relative: str, message: str) -> str:
        self.git("add", "--", relative)
        changed = self.git("diff", "--cached", "--name-only").stdout.splitlines()
        if changed != [relative]:
            raise AssertionError(f"source commit must contain only {relative}, got {changed}")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def push_source(self, branch: str) -> None:
        self.git("push", "--set-upstream", "origin", branch)
