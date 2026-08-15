from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from agent.codex_runner import (
    ProcessResult,
    _default_executor,
    build_codex_command,
    build_codex_env,
    build_implementation_prompt,
    redact_secrets,
    resolve_task,
    run_codex,
)
from agent.config import load_config
from agent.errors import AgentError, ErrorCategory
from agent.spec import parse_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SPEC = REPO_ROOT / "specs" / "tasks" / "example-task.md"


def _spec_and_task():
    spec = parse_spec(EXAMPLE_SPEC)
    return spec, resolve_task(spec, "task-1")


def _last_message_path(command: list[str]) -> Path:
    return Path(command[command.index("--output-last-message") + 1])


def test_command_uses_official_exec_and_workspace_write(tmp_path: Path) -> None:
    command = build_codex_command(last_message_path=tmp_path / "last.txt")
    assert command[0] == "codex"
    assert command[1] == "exec"
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "--output-last-message" in command
    assert "--json" in command
    assert "--ignore-user-config" in command
    assert "--full-auto" not in command
    assert command[-1] == "-"


def test_command_adds_model_when_configured(tmp_path: Path) -> None:
    config = replace(load_config().codex, model="gpt-5.4")
    command = build_codex_command(last_message_path=tmp_path / "last.txt", config=config)
    assert command[command.index("--model") + 1] == "gpt-5.4"


def test_command_rejects_danger_full_access(tmp_path: Path) -> None:
    config = replace(load_config().codex, sandbox="danger-full-access")
    with pytest.raises(AgentError) as exc_info:
        build_codex_command(last_message_path=tmp_path / "last.txt", config=config)
    assert exc_info.value.category is ErrorCategory.POLICY_VIOLATION
    assert exc_info.value.code == "UNSUPPORTED_SANDBOX"


def test_prompt_contains_current_task_and_contract() -> None:
    spec, task = _spec_and_task()
    prompt = build_implementation_prompt(spec, task, repo_root=REPO_ROOT)

    assert "implementation engine" in prompt
    assert "MUST NOT" in prompt
    assert "task-1" in prompt
    assert "Lease repository" in prompt
    assert "worker/**" in prompt
    assert "Acquire is idempotent" in prompt or "idempotent" in prompt.lower()
    assert str(REPO_ROOT) in prompt
    assert "task-2" not in prompt
    assert "Heartbeat loop" not in prompt


def test_env_allowlist_excludes_github_and_openai_keys() -> None:
    env = build_codex_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/runner",
            "CODEX_API_KEY": "codex-secret",
            "OPENAI_API_KEY": "openai-secret",
            "GITHUB_TOKEN": "gh-write-token",
            "GH_TOKEN": "gh-alt",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "DATABASE_URL": "postgres://x",
            "REVIEW_CLASSIFIER_API_KEY": "review-secret",
        }
    )
    assert env["CODEX_API_KEY"] == "codex-secret"
    assert env["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "REVIEW_CLASSIFIER_API_KEY" not in env


def test_secret_redaction() -> None:
    assert redact_secrets("token=codex-secret done", ["codex-secret"]) == "token=[REDACTED] done"


def test_mock_codex_success(tmp_path: Path) -> None:
    spec, task = _spec_and_task()
    captured: dict[str, object] = {}

    def executor(command: list[str], *, cwd: str, env: dict[str, str], timeout: int, stdin: str):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = dict(env)
        captured["timeout"] = timeout
        captured["stdin"] = stdin
        _last_message_path(command).write_text("implemented lease repository\n", encoding="utf-8")
        return ProcessResult(0, '{"type":"item.completed"}\n', "")

    result = run_codex(
        spec,
        task,
        repo_root=REPO_ROOT,
        env={"PATH": "/bin", "CODEX_API_KEY": "codex-secret", "GITHUB_TOKEN": "nope"},
        executor=executor,
    )

    assert result.exit_code == 0
    assert result.final_response == "implemented lease repository\n"
    assert result.metadata["version"] == "0.147.0"
    assert result.metadata["package"] == "@openai/codex"
    assert result.metadata["sandbox"] == "workspace-write"
    assert captured["cwd"] == str(REPO_ROOT)
    assert captured["command"][1] == "exec"
    assert "GITHUB_TOKEN" not in captured["env"]
    assert captured["env"]["CODEX_API_KEY"] == "codex-secret"
    assert "implementation engine" in str(captured["stdin"])


def test_mock_codex_failure_propagates_exit_code() -> None:
    spec, task = _spec_and_task()

    def executor(command: list[str], **_kwargs: object) -> ProcessResult:
        return ProcessResult(2, "", "sandbox denied")

    result = run_codex(spec, task, repo_root=REPO_ROOT, env={"PATH": "/bin"}, executor=executor)
    assert result.exit_code == 2
    assert result.stderr == "sandbox denied"
    assert result.final_response is None


def test_timeout_is_environment_failure() -> None:
    spec, task = _spec_and_task()

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AgentError.environment_failure("codex exec timed out after 1s", code="CODEX_TIMEOUT")

    with pytest.raises(AgentError) as exc_info:
        run_codex(spec, task, repo_root=REPO_ROOT, env={"PATH": "/bin"}, executor=executor)
    assert exc_info.value.category is ErrorCategory.ENVIRONMENT_FAILURE
    assert exc_info.value.code == "CODEX_TIMEOUT"


def test_default_executor_missing_binary() -> None:
    with pytest.raises(AgentError) as exc_info:
        _default_executor(
            ["codex-binary-does-not-exist-xyz"],
            cwd=str(REPO_ROOT),
            env=build_codex_env({"PATH": os.environ.get("PATH", "")}),
            timeout=1,
            stdin="",
        )
    assert exc_info.value.code == "CODEX_NOT_FOUND"


def test_default_executor_timeout() -> None:
    env = build_codex_env({"PATH": os.environ.get("PATH", "")})
    with pytest.raises(AgentError) as exc_info:
        _default_executor(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=str(REPO_ROOT),
            env=env,
            timeout=1,
            stdin="",
        )
    assert exc_info.value.code == "CODEX_TIMEOUT"


def test_run_codex_redacts_api_key_from_output() -> None:
    spec, task = _spec_and_task()

    def executor(command: list[str], **_kwargs: object) -> ProcessResult:
        return ProcessResult(1, "used codex-secret", "err codex-secret")

    result = run_codex(
        spec,
        task,
        repo_root=REPO_ROOT,
        env={"PATH": "/bin", "CODEX_API_KEY": "codex-secret"},
        executor=executor,
    )
    assert "codex-secret" not in result.stdout
    assert "codex-secret" not in result.stderr
    assert "[REDACTED]" in result.stdout
