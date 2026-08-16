"""Official OpenAI Codex CLI runner (implementation engine only).

Verified against OpenAI docs at implementation time (2026-08-15):

- Install / package: https://github.com/openai/codex
  Official npm package is ``@openai/codex`` (not a third-party ``codex-cli``).
  Pinned version: 0.147.0 from https://registry.npmjs.org/@openai/codex/latest
- Non-interactive: ``codex exec``
  https://developers.openai.com/codex/noninteractive
- Workspace write sandbox: ``--sandbox workspace-write``
  Default ``codex exec`` sandbox is read-only. ``--full-auto`` is deprecated.
  https://developers.openai.com/codex/agent-approvals-security
- Final message: ``--output-last-message`` / ``-o``
- JSONL events: ``--json``
- Prompt on stdin: ``codex exec -``
- Hermetic config: ``--ignore-user-config``
- Model override: ``--model`` when configured
  https://developers.openai.com/codex/config-advanced
- CI/API auth: set ``CODEX_API_KEY`` only on the ``codex exec`` process
  https://developers.openai.com/codex/environment-variables
  Do not pass ``OPENAI_API_KEY`` or GitHub write tokens into the subprocess.

This module does not create branches, commit, push, open PRs, update
Execution State, or run validation/repair loops.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import AgentConfig, CodexConfig, load_config
from agent.errors import AgentError
from agent.spec import SpecTask, TaskSpec

INSTRUCTION_PATH = Path(__file__).resolve().parent / "prompts" / "implementation.md"

# Official sandbox names. danger-full-access is documented but not permitted here.
ALLOWED_SANDBOXES = frozenset({"workspace-write", "read-only"})

# Fail-closed: only these names may enter the Codex subprocess environment.
ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "CODEX_CA_CERTIFICATE",
        "SSL_CERT_FILE",
    }
)

DENIED_ENV_ALWAYS = frozenset(
    {
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_PAT",
        "OPENAI_API_KEY",
    }
)

Executor = Callable[..., "ProcessResult"]


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CodexRunResult:
    exit_code: int
    stdout: str
    stderr: str
    final_response: str | None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "final_response": self.final_response,
            "metadata": self.metadata,
        }


def load_implementation_instruction() -> str:
    try:
        return INSTRUCTION_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentError.environment_failure(
            f"implementation prompt not found: {INSTRUCTION_PATH}"
        ) from exc


def build_codex_command(
    *,
    last_message_path: Path,
    config: CodexConfig | None = None,
) -> list[str]:
    cfg = config or load_config().codex
    if cfg.sandbox not in ALLOWED_SANDBOXES:
        raise AgentError.policy_violation(
            f"unsupported Codex sandbox: {cfg.sandbox}",
            code="UNSUPPORTED_SANDBOX",
        )
    command = [
        cfg.bin,
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        cfg.sandbox,
        "--output-last-message",
        str(last_message_path),
        "--json",
    ]
    if cfg.ignore_user_config:
        command.append("--ignore-user-config")
    if cfg.model:
        command.extend(["--model", cfg.model])
    command.append("-")
    return command


def build_implementation_prompt(
    spec: TaskSpec,
    task: SpecTask,
    *,
    repo_root: Path | str,
) -> str:
    instruction = load_implementation_instruction()
    root = Path(repo_root)
    sections = [
        instruction,
        "",
        "# Repository",
        f"- path: {root}",
        f"- spec_id: {spec.id}",
        f"- spec_title: {spec.title}",
        "",
        "# Allowed Paths",
        *_bullet(spec.allowed_paths),
        "",
        "# Forbidden Paths",
        *_bullet(spec.forbidden_paths or ("(none listed)",)),
        "",
        "# Forbidden Actions",
        spec.forbidden_actions.strip(),
        "",
        "# Architecture Invariants",
        spec.architecture_invariants.strip(),
        "",
        "# Current Task",
        f"- id: {task.id}",
        f"- title: {task.title}",
        "",
        "## Requirement",
        task.requirement.strip(),
        "",
        "## Acceptance Criteria",
        task.acceptance_criteria.strip(),
        "",
        "## Validation (informational; Orchestrator will execute this later)",
        task.validation.strip(),
        "",
        "Do not run git commit/push, create pull requests, or edit",
        "specs/, .agent/, agent/, or .github/ unless those paths are allowed.",
    ]
    return "\n".join(sections) + "\n"


def build_codex_env(
    source: Mapping[str, str] | None = None,
    *,
    api_key_env: str = "CODEX_API_KEY",
) -> dict[str, str]:
    incoming = dict(os.environ if source is None else source)
    env: dict[str, str] = {}
    for key, value in incoming.items():
        if key in DENIED_ENV_ALWAYS:
            continue
        if key == api_key_env:
            env[key] = value
            continue
        if key in ENV_ALLOWLIST:
            env[key] = value
    return env


def redact_secrets(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def run_codex(
    spec: TaskSpec,
    task: SpecTask,
    *,
    repo_root: Path | str,
    config: AgentConfig | None = None,
    env: Mapping[str, str] | None = None,
    executor: Executor | None = None,
    prompt: str | None = None,
) -> CodexRunResult:
    cfg = config or load_config()
    root = Path(repo_root)
    if not root.is_dir():
        raise AgentError.invalid_input(f"repository working directory not found: {root}")

    prompt_text = (
        prompt if prompt is not None else build_implementation_prompt(spec, task, repo_root=root)
    )
    child_env = build_codex_env(env, api_key_env=cfg.codex.api_key_env)
    secrets = [child_env[cfg.codex.api_key_env]] if cfg.codex.api_key_env in child_env else []

    with tempfile.TemporaryDirectory(prefix="codex-run-") as tmp:
        last_message_path = Path(tmp) / "last-message.txt"
        command = build_codex_command(last_message_path=last_message_path, config=cfg.codex)
        started = time.monotonic()
        process = (executor or _default_executor)(
            command,
            cwd=str(root),
            env=child_env,
            timeout=cfg.codex.timeout_seconds,
            stdin=prompt_text,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        final_response = None
        if last_message_path.is_file():
            final_response = last_message_path.read_text(encoding="utf-8")

    stdout = redact_secrets(process.stdout, secrets)
    stderr = redact_secrets(process.stderr, secrets)
    if final_response is not None:
        final_response = redact_secrets(final_response, secrets)

    return CodexRunResult(
        exit_code=process.returncode,
        stdout=stdout,
        stderr=stderr,
        final_response=final_response,
        metadata={
            "argv": command,
            "cwd": str(root),
            "sandbox": cfg.codex.sandbox,
            "package": cfg.codex.package,
            "version": cfg.codex.version,
            "model": cfg.codex.model,
            "task_id": task.id,
            "spec_id": spec.id,
            "duration_ms": elapsed_ms,
            "api_key_env_present": cfg.codex.api_key_env in child_env,
        },
    )


def resolve_task(spec: TaskSpec, task_id: str) -> SpecTask:
    for task in spec.tasks:
        if task.id == task_id:
            return task
    raise AgentError.invalid_input(f"task not found in spec: {task_id}")


def _default_executor(
    command: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: int,
    stdin: str,
) -> ProcessResult:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=dict(env),
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AgentError.environment_failure(
            f"Codex CLI not found: {command[0]}. Install official @openai/codex@0.147.0",
            code="CODEX_NOT_FOUND",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AgentError.environment_failure(
            f"codex exec timed out after {timeout}s",
            code="CODEX_TIMEOUT",
        ) from exc
    return ProcessResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def _bullet(values: tuple[str, ...] | list[str]) -> list[str]:
    return [f"- {item}" for item in values]
