from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .observations import ObservationLog


@dataclass
class GitRepo:
    root: Path
    remote: Path
    observations: ObservationLog

    @classmethod
    def create(cls, root: Path, observations: ObservationLog) -> "GitRepo":
        repo = root / "repo"
        remote = root / "remote.git"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        result = cls(repo, remote, observations)
        result.git("config", "user.name", "Phase 6 Test")
        result.git("config", "user.email", "phase6@example.invalid")
        result.git("remote", "add", "origin", str(remote))
        result.write("app/base.txt", "base\n")
        result.write(
            "app/check_exists.py",
            "from pathlib import Path\n"
            "import sys\n"
            "\n"
            "raise SystemExit(0 if Path(sys.argv[1]).is_file() else 1)\n",
        )
        result.write("docs/README.md", "docs\n")
        result.git("add", ".")
        result.git("commit", "-m", "initial")
        result.git("push", "-u", "origin", "main")
        observations.timeline.clear()
        return result

    def git(self, *args: str, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
        effective = dict(os.environ)
        if env:
            effective.update(env)
        self.observations.process("git", ["git", *args], effective)
        return subprocess.run(
            ["git", *args], cwd=self.root, env=effective, check=check, capture_output=True, text=True
        )

    def write(self, relative: str, content: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    @property
    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    @property
    def commit_count(self) -> int:
        return int(self.git("rev-list", "--count", "HEAD").stdout.strip())

    def changed_paths(self, base: str = "HEAD") -> list[str]:
        output = self.git("diff", "--name-only", base).stdout
        return sorted(line for line in output.splitlines() if line)

    def remote_ref(self, branch: str) -> str | None:
        result = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def make_patch(self, changes: dict[str, str]) -> bytes:
        for path, content in changes.items():
            self.write(path, content)
        self.git("add", "-f", "--", *changes.keys())
        patch = self.git("diff", "--cached", "--binary").stdout.encode("utf-8")
        self.git("reset", "--hard", "HEAD")
        self.observations.timeline.clear()
        self.observations.processes.clear()
        return patch


class RecordingProcessRunner:
    """Injectable subprocess boundary for validation and production Git calls."""

    def __init__(self, observations: ObservationLog) -> None:
        self.observations = observations

    def run(self, *, role: str, argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        self.observations.process(role, argv, env)
        return subprocess.run(argv, cwd=cwd, env=env, check=False, capture_output=True, text=True)
