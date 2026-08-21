"""Load orchestrator configuration from a single JSON file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.errors import AgentError, ErrorCategory

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


@dataclass(frozen=True)
class TaskSpecConfig:
    directory: str


@dataclass(frozen=True)
class StateConfig:
    directory: str


@dataclass(frozen=True)
class CodexConfig:
    bin: str
    package: str
    version: str
    model: str | None
    timeout_seconds: int
    sandbox: str
    api_key_env: str
    ignore_user_config: bool


@dataclass(frozen=True)
class RetryConfig:
    repair_attempt_limit: int
    review_attempt_limit: int


@dataclass(frozen=True)
class ValidationConfig:
    timeout_seconds: int
    require_clean_worktree: bool


@dataclass(frozen=True)
class ReviewConfig:
    provider: str
    classifier_model: str
    confidence_threshold: float
    auto_repair_enabled: bool
    api_key_env: str
    track_author: str
    max_comments_per_run: int | None


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool
    channel: str | None
    mention: str | None


@dataclass(frozen=True)
class CodeRabbitConfig:
    actor: str
    check_app_slug: str
    status_context: str


@dataclass(frozen=True)
class AgentConfig:
    task_spec: TaskSpecConfig
    state: StateConfig
    codex: CodexConfig
    retry: RetryConfig
    validation: ValidationConfig
    review: ReviewConfig
    notification: NotificationConfig
    coderabbit: CodeRabbitConfig


def load_config(path: Path | str | None = None) -> AgentConfig:
    """Load and validate config. Missing/unreadable files are EnvironmentFailure."""
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    try:
        raw = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise AgentError(
            ErrorCategory.ENVIRONMENT_FAILURE,
            f"config file not found: {config_path}",
        ) from exc
    except OSError as exc:
        raise AgentError(
            ErrorCategory.ENVIRONMENT_FAILURE,
            f"config file could not be read: {config_path}",
        ) from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentError(
            ErrorCategory.INVALID_INPUT,
            f"config is not valid JSON: {config_path}",
        ) from exc

    if not isinstance(payload, dict):
        raise AgentError.invalid_input("config root must be an object")

    return _parse_config(payload)


def _parse_config(payload: dict[str, Any]) -> AgentConfig:
    task_spec = _require_object(payload, "task_spec")
    state = _require_object(payload, "state")
    codex = _optional_object(payload, "codex")
    retry = _optional_object(payload, "retry")
    validation = _optional_object(payload, "validation")
    review = _optional_object(payload, "review")
    notification = _optional_object(payload, "notification")
    coderabbit = _optional_object(payload, "coderabbit")

    return AgentConfig(
        task_spec=TaskSpecConfig(
            directory=_require_non_empty_str(task_spec, "directory", "task_spec")
        ),
        state=StateConfig(directory=_require_non_empty_str(state, "directory", "state")),
        codex=CodexConfig(
            bin=_optional_non_empty_str(codex, "bin", "codex", default="codex"),
            package=_optional_non_empty_str(codex, "package", "codex", default="@openai/codex"),
            version=_optional_non_empty_str(codex, "version", "codex", default="0.147.0"),
            model=_optional_str(codex, "model", "codex"),
            timeout_seconds=_optional_positive_int(codex, "timeout_seconds", "codex", default=1800),
            sandbox=_optional_non_empty_str(codex, "sandbox", "codex", default="workspace-write"),
            api_key_env=_optional_non_empty_str(
                codex, "api_key_env", "codex", default="CODEX_API_KEY"
            ),
            ignore_user_config=_optional_bool(codex, "ignore_user_config", "codex", default=True),
        ),
        retry=RetryConfig(
            repair_attempt_limit=_optional_non_negative_int(
                retry, "repair_attempt_limit", "retry", default=3
            ),
            review_attempt_limit=_optional_non_negative_int(
                retry, "review_attempt_limit", "retry", default=3
            ),
        ),
        validation=ValidationConfig(
            timeout_seconds=_optional_positive_int(
                validation, "timeout_seconds", "validation", default=600
            ),
            require_clean_worktree=_optional_bool(
                validation, "require_clean_worktree", "validation", default=True
            ),
        ),
        review=ReviewConfig(
            provider=_optional_non_empty_str(review, "provider", "review", default="openai"),
            classifier_model=_optional_non_empty_str(
                review,
                "classifier_model",
                "review",
                default="gpt-5.4-nano-2026-03-17",
            ),
            confidence_threshold=_optional_unit_float(
                review, "confidence_threshold", "review", default=0.80
            ),
            auto_repair_enabled=_optional_bool(
                review, "auto_repair_enabled", "review", default=False
            ),
            api_key_env=_optional_non_empty_str(
                review, "api_key_env", "review", default="REVIEW_CLASSIFIER_API_KEY"
            ),
            track_author=_optional_non_empty_str(
                review, "track_author", "review", default="github-actions[bot]"
            ),
            max_comments_per_run=_optional_int(review, "max_comments_per_run", "review"),
        ),
        notification=NotificationConfig(
            enabled=_optional_bool(notification, "enabled", "notification", default=False),
            channel=_optional_str(notification, "channel", "notification"),
            mention=_optional_str(notification, "mention", "notification"),
        ),
        coderabbit=CodeRabbitConfig(
            actor=_optional_non_empty_str(
                coderabbit, "actor", "coderabbit", default="coderabbitai[bot]"
            ),
            check_app_slug=_optional_non_empty_str(
                coderabbit, "check_app_slug", "coderabbit", default="coderabbitai"
            ),
            status_context=_optional_non_empty_str(
                coderabbit, "status_context", "coderabbit", default="CodeRabbit"
            ),
        ),
    )


def _require_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in payload:
        raise AgentError.invalid_input(f"missing required object: {key}")
    value = payload[key]
    if not isinstance(value, dict):
        raise AgentError.invalid_input(f"{key} must be an object")
    return value


def _optional_object(payload: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in payload or payload[key] is None:
        return {}
    value = payload[key]
    if not isinstance(value, dict):
        raise AgentError.invalid_input(f"{key} must be an object")
    return value


def _require_non_empty_str(obj: dict[str, Any], key: str, prefix: str) -> str:
    if key not in obj:
        raise AgentError.invalid_input(f"missing required field: {prefix}.{key}")
    return _as_non_empty_str(obj[key], f"{prefix}.{key}")


def _optional_non_empty_str(obj: dict[str, Any], key: str, prefix: str, *, default: str) -> str:
    if key not in obj or obj[key] is None:
        return default
    return _as_non_empty_str(obj[key], f"{prefix}.{key}")


def _optional_str(obj: dict[str, Any], key: str, prefix: str) -> str | None:
    if key not in obj or obj[key] is None:
        return None
    value = obj[key]
    if not isinstance(value, str):
        raise AgentError.invalid_input(f"{prefix}.{key} must be a string or null")
    return value


def _optional_int(obj: dict[str, Any], key: str, prefix: str) -> int | None:
    if key not in obj or obj[key] is None:
        return None
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentError.invalid_input(f"{prefix}.{key} must be an integer or null")
    return value


def _optional_positive_int(obj: dict[str, Any], key: str, prefix: str, *, default: int) -> int:
    if key not in obj or obj[key] is None:
        return default
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentError.invalid_input(f"{prefix}.{key} must be an integer")
    if value <= 0:
        raise AgentError.invalid_input(f"{prefix}.{key} must be > 0")
    return value


def _optional_non_negative_int(obj: dict[str, Any], key: str, prefix: str, *, default: int) -> int:
    if key not in obj or obj[key] is None:
        return default
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentError.invalid_input(f"{prefix}.{key} must be an integer")
    if value < 0:
        raise AgentError.invalid_input(f"{prefix}.{key} must be >= 0")
    return value


def _optional_unit_float(obj: dict[str, Any], key: str, prefix: str, *, default: float) -> float:
    if key not in obj or obj[key] is None:
        return default
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise AgentError.invalid_input(f"{prefix}.{key} must be a number")
    number = float(value)
    if number < 0 or number > 1:
        raise AgentError.invalid_input(f"{prefix}.{key} must be between 0 and 1")
    return number


def _optional_bool(obj: dict[str, Any], key: str, prefix: str, *, default: bool) -> bool:
    if key not in obj or obj[key] is None:
        return default
    value = obj[key]
    if not isinstance(value, bool):
        raise AgentError.invalid_input(f"{prefix}.{key} must be a boolean")
    return value


def _as_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentError.invalid_input(f"{field} must be a non-empty string")
    return value
