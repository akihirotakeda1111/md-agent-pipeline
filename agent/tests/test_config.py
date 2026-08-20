from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent.config import load_config
from agent.errors import AgentError, ErrorCategory


def test_load_default_config() -> None:
    config = load_config()

    assert config.task_spec.directory == "specs/tasks"
    assert config.state.directory == ".agent/state"
    assert config.codex.bin == "codex"
    assert config.codex.package == "@openai/codex"
    assert config.codex.version == "0.147.0"
    assert config.codex.sandbox == "workspace-write"
    assert config.codex.api_key_env == "CODEX_API_KEY"
    assert config.validation.timeout_seconds == 600
    assert config.validation.require_clean_worktree is True
    assert config.retry.repair_attempt_limit == 3
    assert config.retry.review_attempt_limit == 3
    assert config.review.confidence_threshold == 0.80
    assert config.review.api_key_env == "REVIEW_CLASSIFIER_API_KEY"
    assert config.review.track_author == "github-actions[bot]"
    assert config.review.max_comments_per_run is None
    assert config.notification.enabled is False
    assert config.notification.mention is None
    assert config.coderabbit.actor == "coderabbitai[bot]"
    assert config.coderabbit.check_app_slug == "coderabbitai"
    assert config.coderabbit.status_context == "CodeRabbit"


def test_load_config_from_explicit_path(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "task_spec": {"directory": "custom/specs"},
                "state": {"directory": "custom/state"},
                "retry": {"repair_attempt_limit": 1, "review_attempt_limit": 0},
                "coderabbit": {"actor": "review-bot"},
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.task_spec.directory == "custom/specs"
    assert config.state.directory == "custom/state"
    assert config.retry.repair_attempt_limit == 1
    assert config.retry.review_attempt_limit == 0
    assert config.coderabbit.actor == "review-bot"
    assert config.coderabbit.check_app_slug == "coderabbitai"
    assert config.coderabbit.status_context == "CodeRabbit"


def test_missing_config_file_is_environment_failure(tmp_path: Path) -> None:
    with pytest.raises(AgentError) as exc_info:
        load_config(tmp_path / "missing.json")

    assert exc_info.value.category is ErrorCategory.ENVIRONMENT_FAILURE


def test_invalid_json_is_invalid_input(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(AgentError) as exc_info:
        load_config(path)

    assert exc_info.value.category is ErrorCategory.INVALID_INPUT


def test_missing_required_section_is_invalid_input(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"task_spec": {"directory": "specs/tasks"}}), encoding="utf-8")

    with pytest.raises(AgentError) as exc_info:
        load_config(path)

    assert exc_info.value.category is ErrorCategory.INVALID_INPUT
    assert "state" in str(exc_info.value)


def test_negative_retry_limit_is_invalid_input(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "task_spec": {"directory": "specs/tasks"},
                "state": {"directory": ".agent/state"},
                "retry": {"repair_attempt_limit": -1},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentError) as exc_info:
        load_config(path)

    assert exc_info.value.category is ErrorCategory.INVALID_INPUT
