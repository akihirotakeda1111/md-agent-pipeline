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

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from agent.config import AgentConfig, CodexConfig, RuntimeEditPolicy, load_config
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
        "AGENT_PR_PAT",
        "OPENAI_API_KEY",
    }
)

_SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")

DIAGNOSTIC_STAGES = frozenset({"implementation", "repair"})
DIAGNOSTIC_MAX_CHARS = 4096
DIAGNOSTIC_MAX_LINES = 40
_ERROR_EVENT_TYPES = frozenset(
    {
        "error",
        "turn.failed",
        "item.failed",
        "thread.failed",
    }
)
_SECRET_ASSIGN = re.compile(
    r"(?P<prefix>\b(?:CODEX_API_KEY|OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|GITHUB_PAT|AGENT_PR_PAT)"
    r"[ \t]*[=:][ \t]*)(?P<value>[^\s\"']+)",
    re.IGNORECASE,
)
_SK_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_GITHUB_TOKEN_VALUE = re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]+")

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
    diagnostic: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "final_response": self.final_response,
            "metadata": self.metadata,
        }
        if self.diagnostic is not None:
            payload["diagnostic"] = self.diagnostic
        return payload


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
    runtime_policy: RuntimeEditPolicy,
) -> str:
    from agent.scope import format_scope_prompt_sections

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
        format_scope_prompt_sections(spec, runtime_policy),
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
        "Do not run git commit/push or create pull requests.",
        "Protected paths cannot be edited even when listed in Allowed Paths.",
    ]
    return "\n".join(sections) + "\n"


def build_allowlisted_env(
    source: Mapping[str, str] | None = None,
    *,
    extra_allow: frozenset[str] = frozenset(),
    include_keys: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Build a fail-closed env. Secrets are omitted unless in include_keys."""
    incoming = dict(os.environ if source is None else source)
    allow = ENV_ALLOWLIST | extra_allow
    env: dict[str, str] = {}
    for key, value in incoming.items():
        if key in include_keys:
            env[key] = value
            continue
        if key in DENIED_ENV_ALWAYS:
            continue
        if key.endswith(_SECRET_SUFFIXES):
            continue
        if "API_KEY" in key:
            continue
        if key in allow:
            env[key] = value
    return env


def build_codex_env(
    source: Mapping[str, str] | None = None,
    *,
    api_key_env: str = "CODEX_API_KEY",
) -> dict[str, str]:
    """Allowlist plus the Codex credential. Other subprocesses do not inherit it."""
    return build_allowlisted_env(source, include_keys=frozenset({api_key_env}))


def detach_codex_api_key(
    source: Mapping[str, str] | None = None,
    *,
    api_key_env: str = "CODEX_API_KEY",
) -> tuple[Mapping[str, str] | None, str | None]:
    """Remove the Codex credential from os.environ and from ``source``.

    Returns ``(source_without_key, key_value)``. ``source is None`` means
    subsequent subprocesses should read the scrubbed process environment.
    """
    os_value = os.environ.pop(api_key_env, None)
    if source is None:
        return None, os_value
    copied = dict(source)
    source_value = copied.pop(api_key_env, None)
    return copied, source_value if source_value is not None else os_value


def attach_codex_api_key(
    source: Mapping[str, str] | None,
    api_key: str | None,
    *,
    api_key_env: str = "CODEX_API_KEY",
) -> Mapping[str, str] | None:
    """Return an env mapping for the Codex subprocess only."""
    if api_key is None:
        return source
    payload = dict(os.environ if source is None else source)
    payload[api_key_env] = api_key
    return payload


def redact_secrets(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def bound_diagnostic_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if len(lines) > DIAGNOSTIC_MAX_LINES:
        lines = lines[-DIAGNOSTIC_MAX_LINES:]
    joined = "\n".join(lines).strip()
    if len(joined) > DIAGNOSTIC_MAX_CHARS:
        joined = joined[-DIAGNOSTIC_MAX_CHARS:]
    return joined


def sanitize_diagnostic_text(text: str, secrets: list[str]) -> str:
    redacted = redact_secrets(text, secrets)
    redacted = _SECRET_ASSIGN.sub(lambda match: f"{match.group('prefix')}[REDACTED]", redacted)
    redacted = _SK_TOKEN.sub("[REDACTED]", redacted)
    redacted = _GITHUB_TOKEN_VALUE.sub("[REDACTED]", redacted)
    return bound_diagnostic_text(redacted)


def _text_from_error_value(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        for key in ("message", "error", "reason", "detail"):
            inner = _text_from_error_value(value.get(key))
            if inner:
                return inner
    return None


def error_text_from_jsonl_event(event: dict[str, Any]) -> str | None:
    type_name = str(event.get("type") or event.get("event") or "").strip().lower()
    typed_error = (
        type_name in _ERROR_EVENT_TYPES
        or type_name.endswith(".error")
        or type_name.endswith(".failed")
    )
    extracted = _text_from_error_value(event.get("error"))
    if extracted is None and typed_error:
        extracted = _text_from_error_value(event.get("message"))
    if extracted:
        return extracted
    payload = event.get("payload")
    if isinstance(payload, dict):
        return error_text_from_jsonl_event(payload)
    return None


def extract_jsonl_error(text: str) -> str | None:
    last: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            message = error_text_from_jsonl_event(parsed)
            if message:
                last = message
    return last


def extract_codex_error_diagnostic(stdout: str, stderr: str) -> tuple[str | None, str]:
    jsonl_error = extract_jsonl_error(stdout) or extract_jsonl_error(stderr)
    if jsonl_error:
        return "jsonl", jsonl_error
    fallback = stderr.strip()
    return ("stderr" if fallback else None, fallback)


def resolve_diagnostic_stage(stage: str) -> str:
    return stage if stage in DIAGNOSTIC_STAGES else "implementation"


def build_codex_diagnostic(
    *,
    exit_code: int,
    duration_ms: int,
    stage: str,
    attempt: int,
    api_key_env_present: bool,
    stdout: str,
    stderr: str,
    secrets: list[str],
) -> dict[str, Any]:
    source, raw_error = extract_codex_error_diagnostic(stdout, stderr)
    return {
        "event": "codex.diagnostic",
        "exit_code": exit_code,
        "duration_ms": duration_ms,
        "stage": resolve_diagnostic_stage(stage),
        "attempt": max(0, attempt),
        "api_key_env_present": bool(api_key_env_present),
        "error_source": source,
        "error": sanitize_diagnostic_text(raw_error, secrets),
    }


def build_post_codex_diagnostic(
    *,
    exit_code: int,
    changed_paths: tuple[str, ...] | list[str],
    stage: str,
    attempt: int,
    final_message: str | None,
    secrets: list[str],
) -> dict[str, Any]:
    return {
        "event": "codex.diagnostic",
        "exit_code": exit_code,
        "changed_paths": list(changed_paths),
        "stage": resolve_diagnostic_stage(stage),
        "attempt": max(0, attempt),
        "final_message": sanitize_diagnostic_text(final_message or "", secrets),
    }


def emit_codex_diagnostic(
    diagnostic: dict[str, Any],
    *,
    stream: TextIO | None = None,
) -> None:
    output = stream or sys.stderr
    output.write(json.dumps(diagnostic, ensure_ascii=False) + "\n")
    output.flush()


def run_codex(
    spec: TaskSpec,
    task: SpecTask,
    *,
    repo_root: Path | str,
    config: AgentConfig | None = None,
    env: Mapping[str, str] | None = None,
    executor: Executor | None = None,
    prompt: str | None = None,
    stage: str = "implementation",
    attempt: int = 0,
) -> CodexRunResult:
    cfg = config or load_config()
    root = Path(repo_root)
    if not root.is_dir():
        raise AgentError.invalid_input(f"repository working directory not found: {root}")

    prompt_text = (
        prompt
        if prompt is not None
        else build_implementation_prompt(
            spec, task, repo_root=root, runtime_policy=cfg.runtime_edit_policy
        )
    )
    child_env = build_codex_env(env, api_key_env=cfg.codex.api_key_env)
    api_key_value = child_env.get(cfg.codex.api_key_env)
    secrets = [api_key_value] if api_key_value else []
    api_key_env_present = bool((api_key_value or "").strip())

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

    diagnostic = None
    if process.returncode != 0:
        diagnostic = build_codex_diagnostic(
            exit_code=process.returncode,
            duration_ms=elapsed_ms,
            stage=stage,
            attempt=attempt,
            api_key_env_present=api_key_env_present,
            stdout=stdout,
            stderr=stderr,
            secrets=secrets,
        )
        emit_codex_diagnostic(diagnostic)

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
            "api_key_env_present": api_key_env_present,
        },
        diagnostic=diagnostic,
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
