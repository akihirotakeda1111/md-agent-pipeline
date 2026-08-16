from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from agent.classify import FailureClass, classify_validation
from agent.errors import AgentError
from agent.validation import (
    build_validation_env,
    extract_commands,
    inspect_command,
    parse_command,
    run_validation_command,
    run_validation_text,
)


def _env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "")}
    if os.environ.get("PATHEXT"):
        env["PATHEXT"] = os.environ["PATHEXT"]
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def test_extracts_fenced_validation_commands() -> None:
    text = """
```text
pytest worker/tests/test_lease_repository.py
python -m ruff check worker
```
"""
    assert extract_commands(text) == [
        "pytest worker/tests/test_lease_repository.py",
        "python -m ruff check worker",
    ]


def test_validation_success_and_failure(tmp_path: Path) -> None:
    env = _env()
    (tmp_path / "ok.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "bad.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    ok = run_validation_command(
        f"{sys.executable} ok.py",
        repo_root=tmp_path,
        task_id="task-1",
        timeout_seconds=30,
        env=env,
    )
    assert ok.passed is True
    assert ok.exit_code == 0
    assert ok.task_id == "task-1"

    bad = run_validation_command(
        f"{sys.executable} bad.py",
        repo_root=tmp_path,
        task_id="task-1",
        timeout_seconds=30,
        env=env,
    )
    assert bad.passed is False
    assert bad.exit_code == 2


def test_validation_timeout(tmp_path: Path) -> None:
    env = _env()
    (tmp_path / "sleep.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    record = run_validation_command(
        f"{sys.executable} sleep.py",
        repo_root=tmp_path,
        task_id="task-1",
        timeout_seconds=1,
        env=env,
    )
    assert record.timed_out is True
    assert record.passed is False
    assert classify_validation(record) is FailureClass.ENVIRONMENT_FAILURE


def test_forbidden_and_unknown_commands_are_denied() -> None:
    assert inspect_command(parse_command("terraform apply")) is not None
    assert inspect_command(parse_command("git push origin main")) is not None
    assert inspect_command(parse_command("curl https://example.com")) is not None
    assert inspect_command(parse_command("pytest tests")) is None


def test_validation_stops_at_first_failure(tmp_path: Path) -> None:
    env = _env()
    (tmp_path / "fail.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("print(2)\n", encoding="utf-8")
    records = run_validation_text(
        f"```text\n{sys.executable} fail.py\n{sys.executable} ok.py\n```",
        repo_root=tmp_path,
        task_id="task-1",
        timeout_seconds=30,
        env=env,
    )
    assert len(records) == 1
    assert records[0].passed is False


def test_default_validation_env_keeps_path_and_strips_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", os.environ.get("PATH", "C:\\Windows\\System32"))
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "secret-key")
    env = build_validation_env()
    assert "PATH" in env
    assert "GITHUB_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_empty_validation_is_invalid(tmp_path: Path) -> None:
    with pytest.raises(AgentError):
        run_validation_text("   \n", repo_root=tmp_path, task_id="task-1", timeout_seconds=1)
