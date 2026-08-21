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
    def create(cls, parent: Path, observations: ObservationLog) -> GitRepo:
        root = parent / "repo"
        remote = parent / "remote.git"
        root.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "init", "-b", "main", str(root)], check=True, capture_output=True, text=True
        )
        result = cls(root, remote, observations)
        result.git("config", "user.name", "Phase 7 Test")
        result.git("config", "user.email", "phase7@example.invalid")
        result.git("remote", "add", "origin", str(remote))
        result.write("app/review.txt", "before\n")
        result.write("app/task-one.txt", "ready\n")
        result.write("app/task-two.txt", "ready\n")
        result.write(
            "app/check_content.py",
            (
                "from pathlib import Path\nimport sys\n"
                "raise SystemExit("
                "0 if Path(sys.argv[1]).read_text().strip() == sys.argv[2] else 1)\n"
            ),
        )
        result.write("docs/README.md", "outside allowed scope\n")
        result.git("add", ".")
        result.git("commit", "-m", "initial")
        result.git("push", "-u", "origin", "main")
        result.git("checkout", "-b", "agent/phase7-integration")
        result.git("push", "-u", "origin", "agent/phase7-integration")
        observations.timeline.clear()
        observations.processes.clear()
        return result

    def git(
        self, *args: str, env: dict[str, str] | None = None, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        effective = dict(os.environ)
        if env:
            effective.update(env)
        self.observations.process("git", ["git", *args], effective)
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=effective,
            check=check,
            capture_output=True,
            text=True,
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

    def remote_ref(self, branch: str = "agent/phase7-integration") -> str | None:
        completed = subprocess.run(
            ["git", "--git-dir", str(self.remote), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() if completed.returncode == 0 else None

    def changed_paths(self) -> list[str]:
        return sorted(
            line[3:] for line in self.git("status", "--short").stdout.splitlines() if line
        )


class RecordingProcessRunner:
    def __init__(self, observations: ObservationLog) -> None:
        self.observations = observations

    def run(
        self, *, role: str, argv: list[str], cwd: Path, env: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.observations.process(role, argv, env)
        return subprocess.run(argv, cwd=cwd, env=env, check=False, capture_output=True, text=True)
