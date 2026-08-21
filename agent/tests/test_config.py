from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent.config import load_config
from agent.errors import AgentError, ErrorCategory

DEFAULT_PROTECTED = ["specs/**", ".agent/**", "agent/**", ".github/**"]


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_spec": {"directory": "specs/tasks"},
        "state": {"directory": ".agent/state"},
        "runtime_edit_policy": {"protected_paths": list(DEFAULT_PROTECTED)},
    }
    payload.update(overrides)
    return payload


def _write(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _reject(tmp_path: Path, payload: dict[str, Any], needle: str) -> None:
    with pytest.raises(AgentError) as exc_info:
        load_config(_write(tmp_path, payload))
    assert exc_info.value.category is ErrorCategory.INVALID_INPUT
    assert needle in str(exc_info.value)


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
    assert config.review.auto_repair_enabled is False
    assert config.review.api_key_env == "REVIEW_CLASSIFIER_API_KEY"
    assert config.review.track_author == "github-actions[bot]"
    assert config.review.max_comments_per_run is None
    assert config.notification.enabled is False
    assert config.notification.mention is None
    assert config.coderabbit.actor == "coderabbitai[bot]"
    assert config.coderabbit.check_app_slug == "coderabbitai"
    assert config.coderabbit.status_context == "CodeRabbit"
    assert config.runtime_edit_policy.protected_paths == (
        "specs/**",
        ".agent/**",
        "agent/**",
        ".github/**",
    )


def test_load_config_from_explicit_path(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _payload(
            task_spec={"directory": "custom/specs"},
            state={"directory": "custom/state"},
            retry={"repair_attempt_limit": 1, "review_attempt_limit": 0},
            coderabbit={"actor": "review-bot"},
            runtime_edit_policy={
                "protected_paths": [*DEFAULT_PROTECTED, "custom/specs/**", "custom/state/**"]
            },
        ),
    )

    config = load_config(path)

    assert config.task_spec.directory == "custom/specs"
    assert config.state.directory == "custom/state"
    assert config.retry.repair_attempt_limit == 1
    assert config.retry.review_attempt_limit == 0
    assert config.coderabbit.actor == "review-bot"
    assert config.coderabbit.check_app_slug == "coderabbitai"
    assert config.coderabbit.status_context == "CodeRabbit"
    assert config.review.auto_repair_enabled is False
    assert "custom/specs/**" in config.runtime_edit_policy.protected_paths


def test_auto_repair_enabled_can_be_turned_on(tmp_path: Path) -> None:
    path = _write(tmp_path, _payload(review={"auto_repair_enabled": True}))
    assert load_config(path).review.auto_repair_enabled is True


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
    _reject(tmp_path, _payload(retry={"repair_attempt_limit": -1}), "retry.repair_attempt_limit")


def test_runtime_edit_policy_is_required(tmp_path: Path) -> None:
    payload = _payload()
    del payload["runtime_edit_policy"]
    _reject(tmp_path, payload, "runtime_edit_policy")


def test_protected_paths_are_required(tmp_path: Path) -> None:
    _reject(tmp_path, _payload(runtime_edit_policy={}), "protected_paths")


def test_empty_protected_paths_are_rejected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": []}),
        "must not be empty",
    )


def test_absolute_protected_path_is_rejected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": [*DEFAULT_PROTECTED, "/etc/passwd"]}),
        "repository-relative",
    )


def test_windows_drive_protected_path_is_rejected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": [*DEFAULT_PROTECTED, "C:\\secrets"]}),
        "Windows drive",
    )


def test_unc_protected_path_is_rejected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(
            runtime_edit_policy={"protected_paths": [*DEFAULT_PROTECTED, "\\\\server\\share"]}
        ),
        "UNC",
    )


def test_dot_and_dotdot_segments_are_rejected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": [*DEFAULT_PROTECTED, "specs/../agent"]}),
        ". or ..",
    )
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": [*DEFAULT_PROTECTED, "./agent/**"]}),
        ". or ..",
    )


def test_config_file_must_be_protected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": ["specs/**", ".agent/**", ".github/**"]}),
        "agent/config.json",
    )


def test_task_spec_directory_must_be_protected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": [".agent/**", "agent/**", ".github/**"]}),
        "task_spec.directory",
    )


def test_state_directory_must_be_protected(tmp_path: Path) -> None:
    _reject(
        tmp_path,
        _payload(runtime_edit_policy={"protected_paths": ["specs/**", "agent/**", ".github/**"]}),
        "state.directory",
    )


def test_protected_paths_are_canonicalized_and_deduplicated(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            _payload(
                runtime_edit_policy={
                    "protected_paths": [
                        "specs/**",
                        "agent\\**",
                        "agent/**",
                        ".agent/**",
                        ".github/**",
                    ]
                }
            ),
        )
    )
    assert config.runtime_edit_policy.protected_paths == (
        "specs/**",
        "agent/**",
        ".agent/**",
        ".github/**",
    )


@pytest.mark.parametrize("section", ["task_spec", "state"])
@pytest.mark.parametrize(
    ("directory", "needle"),
    [
        ("/absolute/path", "repository-relative"),
        ("/specs/tasks", "repository-relative"),
        ("//server/share", "UNC"),
        ("\\\\server\\share", "UNC"),
        ("C:\\path", "Windows drive"),
        ("../outside", ". or .."),
        ("directory/../outside", ". or .."),
    ],
)
def test_config_directory_must_be_repository_relative(
    tmp_path: Path, section: str, directory: str, needle: str
) -> None:
    payload = _payload()
    payload[section] = {"directory": directory}
    _reject(tmp_path, payload, needle)


def test_config_directories_accept_repository_relative_paths(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            _payload(
                task_spec={"directory": "specs/tasks/"},
                state={"directory": ".agent/state/"},
            ),
        )
    )
    assert config.task_spec.directory == "specs/tasks"
    assert config.state.directory == ".agent/state"
