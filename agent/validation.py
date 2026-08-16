"""Orchestrator-owned validation command runner."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent.codex_runner import build_allowlisted_env
from agent.errors import AgentError

FENCE_RE = re.compile(r"```(?:[A-Za-z0-9_-]+)?\r?\n(.*?)```", re.DOTALL)

ALLOWED_COMMANDS = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "py",
        "ruff",
        "mypy",
        "pyright",
        "npm",
        "npx",
        "node",
        "cargo",
        "go",
        "make",
    }
)

FORBIDDEN_COMMANDS = frozenset(
    {
        "terraform",
        "sudo",
        "rm",
        "rmdir",
        "del",
        "chmod",
        "chown",
        "kubectl",
        "aws",
        "az",
        "gcloud",
    }
)

FORBIDDEN_GIT_SUBCOMMANDS = frozenset(
    {
        "push",
        "commit",
        "rebase",
        "reset",
        "merge",
        "checkout",
        "branch",
        "tag",
        "stash",
        "clean",
        "am",
        "cherry-pick",
    }
)

VALIDATION_ENV_EXTRA = frozenset({"PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME", "NODE_PATH"})


@dataclass(frozen=True)
class ParsedCommand:
    argv: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class ValidationRecord:
    task_id: str
    command: str
    argv: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    denied: bool
    deny_reason: str | None = None

    @property
    def passed(self) -> bool:
        return not self.denied and not self.timed_out and self.exit_code == 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "command": self.command,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "timed_out": self.timed_out,
            "denied": self.denied,
            "deny_reason": self.deny_reason,
            "passed": self.passed,
        }


def extract_commands(text: str) -> list[str]:
    commands: list[str] = []
    for block in FENCE_RE.findall(text):
        commands.extend(_lines_to_commands(block))
    if commands:
        return commands
    return _lines_to_commands(text)


def parse_command(command: str) -> ParsedCommand:
    try:
        argv = tuple(shlex.split(command, posix=(os.name != "nt")))
    except ValueError as exc:
        raise AgentError.invalid_input(f"invalid validation command: {command}") from exc
    if not argv:
        raise AgentError.invalid_input("validation command is empty")
    return ParsedCommand(argv=argv, source=command)


def inspect_command(parsed: ParsedCommand) -> str | None:
    binary = Path(parsed.argv[0]).name.lower()
    if binary.endswith(".exe"):
        binary = binary[:-4]
    if binary in FORBIDDEN_COMMANDS:
        return f"forbidden command: {binary}"
    if binary == "git":
        sub = parsed.argv[1].lower() if len(parsed.argv) > 1 else ""
        if sub in FORBIDDEN_GIT_SUBCOMMANDS or not sub:
            return f"forbidden git subcommand: {sub or '(missing)'}"
        return "git is not a permitted validation command"
    if binary not in ALLOWED_COMMANDS:
        return f"command not in allowlist: {binary}"
    joined = " ".join(parsed.argv).lower()
    if "terraform apply" in joined or "terraform destroy" in joined:
        return "destructive terraform command"
    return None


def build_validation_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    return build_allowlisted_env(source, extra_allow=VALIDATION_ENV_EXTRA)


def run_validation_command(
    command: str,
    *,
    repo_root: Path | str,
    task_id: str,
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> ValidationRecord:
    parsed = parse_command(command)
    denied = inspect_command(parsed)
    if denied:
        return ValidationRecord(
            task_id=task_id,
            command=command,
            argv=parsed.argv,
            exit_code=None,
            stdout="",
            stderr=denied,
            duration_ms=0,
            timed_out=False,
            denied=True,
            deny_reason=denied,
        )
    child_env = build_validation_env(env)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(parsed.argv),
            cwd=str(repo_root),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentError.environment_failure(
            f"validation executable not found: {parsed.argv[0]}",
            code="VALIDATION_NOT_FOUND",
        ) from exc
    except subprocess.TimeoutExpired:
        duration_ms = int((time.monotonic() - started) * 1000)
        return ValidationRecord(
            task_id=task_id,
            command=command,
            argv=parsed.argv,
            exit_code=None,
            stdout="",
            stderr=f"validation timed out after {timeout_seconds}s",
            duration_ms=duration_ms,
            timed_out=True,
            denied=False,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    return ValidationRecord(
        task_id=task_id,
        command=command,
        argv=parsed.argv,
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_ms=duration_ms,
        timed_out=False,
        denied=False,
    )


def run_validation_text(
    text: str,
    *,
    repo_root: Path | str,
    task_id: str,
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> list[ValidationRecord]:
    commands = extract_commands(text)
    if not commands:
        raise AgentError.invalid_input(f"no validation commands found for {task_id}")
    records: list[ValidationRecord] = []
    for command in commands:
        record = run_validation_command(
            command,
            repo_root=repo_root,
            task_id=task_id,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        records.append(record)
        if not record.passed:
            break
    return records


def _lines_to_commands(text: str) -> list[str]:
    commands: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        commands.append(line)
    return commands
