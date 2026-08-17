from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from agent.codex_runner import (
    ProcessResult,
    _default_executor,
    attach_codex_api_key,
    bound_diagnostic_text,
    build_allowlisted_env,
    build_codex_command,
    build_codex_diagnostic,
    build_codex_env,
    build_implementation_prompt,
    build_post_codex_diagnostic,
    detach_codex_api_key,
    extract_jsonl_error,
    redact_secrets,
    resolve_task,
    run_codex,
    sanitize_diagnostic_text,
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


def test_non_codex_allowlist_excludes_codex_api_key() -> None:
    env = build_allowlisted_env(
        {
            "PATH": "/usr/bin",
            "CODEX_API_KEY": "codex-secret",
            "GITHUB_TOKEN": "gh-write-token",
            "PYTHONPATH": "/opt/app",
        }
    )
    assert env["PATH"] == "/usr/bin"
    assert "CODEX_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "PYTHONPATH" not in env


def test_detach_codex_api_key_scrubs_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-secret")
    rest, key = detach_codex_api_key()
    assert rest is None
    assert key == "codex-secret"
    assert "CODEX_API_KEY" not in os.environ
    attached = attach_codex_api_key(rest, key)
    assert attached is not None
    assert attached["CODEX_API_KEY"] == "codex-secret"
    assert "CODEX_API_KEY" not in os.environ


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
    assert result.diagnostic is None
    assert "diagnostic" not in result.to_json_dict()
    assert result.final_response == "implemented lease repository\n"
    assert result.metadata["version"] == "0.147.0"
    assert result.metadata["package"] == "@openai/codex"
    assert result.metadata["sandbox"] == "workspace-write"
    assert captured["cwd"] == str(REPO_ROOT)
    assert captured["command"][1] == "exec"
    assert "GITHUB_TOKEN" not in captured["env"]
    assert captured["env"]["CODEX_API_KEY"] == "codex-secret"
    assert "implementation engine" in str(captured["stdin"])


def test_mock_codex_failure_propagates_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    spec, task = _spec_and_task()

    def executor(command: list[str], **_kwargs: object) -> ProcessResult:
        return ProcessResult(2, "", "sandbox denied")

    result = run_codex(spec, task, repo_root=REPO_ROOT, env={"PATH": "/bin"}, executor=executor)
    assert result.exit_code == 2
    assert result.stderr == "sandbox denied"
    assert result.final_response is None
    assert result.diagnostic is not None
    assert result.diagnostic["event"] == "codex.diagnostic"
    assert result.diagnostic["exit_code"] == 2
    assert result.diagnostic["stage"] == "implementation"
    assert result.diagnostic["attempt"] == 0
    assert result.diagnostic["api_key_env_present"] is False
    assert result.diagnostic["error_source"] == "stderr"
    assert result.diagnostic["error"] == "sandbox denied"
    emitted = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert emitted["error"] == "sandbox denied"


def test_jsonl_error_is_preferred_over_stderr() -> None:
    stdout = "\n".join(
        [
            '{"type":"item.completed","item":{"type":"agent_message"}}',
            '{"type":"error","message":"landlock: sandbox setup failed"}',
        ]
    )
    diagnostic = build_codex_diagnostic(
        exit_code=1,
        duration_ms=12,
        stage="repair",
        attempt=2,
        api_key_env_present=True,
        stdout=stdout,
        stderr="ignore this stderr",
        secrets=[],
    )
    assert diagnostic["stage"] == "repair"
    assert diagnostic["attempt"] == 2
    assert diagnostic["error_source"] == "jsonl"
    assert diagnostic["error"] == "landlock: sandbox setup failed"
    assert extract_jsonl_error(stdout) == "landlock: sandbox setup failed"


def test_diagnostic_redacts_secrets_and_bounds_output() -> None:
    secret = "codex-secret"
    long_stderr = "\n".join(f"line-{index} {secret}" for index in range(80))
    diagnostic = build_codex_diagnostic(
        exit_code=1,
        duration_ms=9,
        stage="implementation",
        attempt=0,
        api_key_env_present=True,
        stdout="",
        stderr=long_stderr,
        secrets=[secret],
    )
    assert secret not in diagnostic["error"]
    assert diagnostic["error"].count("\n") < 80
    assert len(diagnostic["error"]) <= 4096
    assert "CODEX_API_KEY=[REDACTED]" in sanitize_diagnostic_text(
        "CODEX_API_KEY=codex-secret", [secret]
    )
    assert bound_diagnostic_text("\n".join(str(i) for i in range(50))).count("\n") == 39


def test_run_codex_emits_repair_diagnostic(capsys: pytest.CaptureFixture[str]) -> None:
    spec, task = _spec_and_task()

    def executor(command: list[str], **_kwargs: object) -> ProcessResult:
        return ProcessResult(
            1,
            json.dumps({"type": "turn.failed", "error": {"message": "auth codex-secret"}}),
            "",
        )

    result = run_codex(
        spec,
        task,
        repo_root=REPO_ROOT,
        env={"PATH": "/bin", "CODEX_API_KEY": "codex-secret"},
        executor=executor,
        stage="repair",
        attempt=1,
    )
    assert result.diagnostic is not None
    assert result.diagnostic["stage"] == "repair"
    assert result.diagnostic["attempt"] == 1
    assert result.diagnostic["api_key_env_present"] is True
    assert result.diagnostic["error_source"] == "jsonl"
    assert "codex-secret" not in result.diagnostic["error"]
    assert "codex-secret" not in capsys.readouterr().err


def test_post_codex_diagnostic_includes_changed_paths_and_redacts_message() -> None:
    diagnostic = build_post_codex_diagnostic(
        exit_code=0,
        changed_paths=(),
        stage="implementation",
        attempt=0,
        final_message="Inspected repo. CODEX_API_KEY=codex-secret. No changes.",
        secrets=["codex-secret"],
    )
    assert diagnostic["event"] == "codex.diagnostic"
    assert diagnostic["exit_code"] == 0
    assert diagnostic["changed_paths"] == []
    assert diagnostic["stage"] == "implementation"
    assert "codex-secret" not in diagnostic["final_message"]
    assert "No changes" in diagnostic["final_message"]


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
