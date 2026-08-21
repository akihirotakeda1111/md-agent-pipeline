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


CONFIG_RELATIVE_PATH = "agent/config.json"


@dataclass(frozen=True)
class RuntimeEditPolicy:
    protected_paths: tuple[str, ...]


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
    runtime_edit_policy: RuntimeEditPolicy


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
    task_spec_directory = _canonicalize_repo_relative_posix(
        _require_non_empty_str(task_spec, "directory", "task_spec"),
        field="task_spec.directory",
    )
    state_directory = _canonicalize_repo_relative_posix(
        _require_non_empty_str(state, "directory", "state"),
        field="state.directory",
    )
    runtime_edit_policy = _parse_runtime_edit_policy(
        payload,
        task_spec_directory=task_spec_directory,
        state_directory=state_directory,
    )
    codex = _optional_object(payload, "codex")
    retry = _optional_object(payload, "retry")
    validation = _optional_object(payload, "validation")
    review = _optional_object(payload, "review")
    notification = _optional_object(payload, "notification")
    coderabbit = _optional_object(payload, "coderabbit")

    return AgentConfig(
        task_spec=TaskSpecConfig(directory=task_spec_directory),
        state=StateConfig(directory=state_directory),
        runtime_edit_policy=runtime_edit_policy,
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


def _parse_runtime_edit_policy(
    payload: dict[str, Any],
    *,
    task_spec_directory: str,
    state_directory: str,
) -> RuntimeEditPolicy:
    policy = _require_object(payload, "runtime_edit_policy")
    if "protected_paths" not in policy:
        raise AgentError.invalid_input(
            "missing required field: runtime_edit_policy.protected_paths"
        )
    raw_paths = policy["protected_paths"]
    if not isinstance(raw_paths, list):
        raise AgentError.invalid_input("runtime_edit_policy.protected_paths must be an array")
    if not raw_paths:
        raise AgentError.invalid_input("runtime_edit_policy.protected_paths must not be empty")

    canonical: list[str] = []
    seen: set[str] = set()
    for item in raw_paths:
        pattern = _canonicalize_protected_pattern(item)
        if pattern in seen:
            continue
        seen.add(pattern)
        canonical.append(pattern)
    if not canonical:
        raise AgentError.invalid_input("runtime_edit_policy.protected_paths must not be empty")

    protected = tuple(canonical)
    _assert_required_protection(
        protected,
        task_spec_directory=task_spec_directory,
        state_directory=state_directory,
    )
    return RuntimeEditPolicy(protected_paths=protected)


def _canonicalize_protected_pattern(value: Any) -> str:
    field = "runtime_edit_policy.protected_paths"
    if not isinstance(value, str) or not value.strip():
        raise AgentError.invalid_input(f"{field} entries must be non-empty strings")
    return _canonicalize_repo_relative_posix(value, field=field)


def _canonicalize_repo_relative_posix(value: str, *, field: str) -> str:
    """Normalize a repository-relative path or glob without stripping a leading slash.

    Order: trim whitespace, convert backslashes to POSIX separators, then reject
    absolute / drive / UNC / empty / ``.`` / ``..`` segments. Only a trailing
    slash is removed after those checks.
    """
    raw = value.strip()
    if not raw:
        raise AgentError.invalid_input(f"{field} must be a non-empty string")

    normalized = raw.replace("\\", "/")
    if _is_windows_drive_path(raw) or _is_windows_drive_path(normalized):
        raise AgentError.invalid_input(f"{field} must not contain a Windows drive path: {value}")
    if _is_unc_path(raw) or _is_unc_path(normalized):
        raise AgentError.invalid_input(f"{field} must not contain a UNC path: {value}")
    if raw.startswith("/") or normalized.startswith("/"):
        raise AgentError.invalid_input(f"{field} must be repository-relative: {value}")

    parts = normalized.split("/")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts or any(part == "" for part in parts):
        raise AgentError.invalid_input(f"{field} must be repository-relative: {value}")
    if any(part in {".", ".."} for part in parts):
        raise AgentError.invalid_input(f"{field} must not contain . or .. path segments: {value}")
    return "/".join(parts)


def _is_windows_drive_path(value: str) -> bool:
    return len(value) >= 2 and value[0].isalpha() and value[1] == ":"


def _is_unc_path(value: str) -> bool:
    return value.startswith("\\\\") or value.startswith("//")


def _assert_required_protection(
    protected_paths: tuple[str, ...],
    *,
    task_spec_directory: str,
    state_directory: str,
) -> None:
    from agent.scope import path_matches

    if not any(path_matches(CONFIG_RELATIVE_PATH, pattern) for pattern in protected_paths):
        raise AgentError.invalid_input(
            f"runtime_edit_policy.protected_paths must protect {CONFIG_RELATIVE_PATH}"
        )
    if not _directory_is_recursively_protected(task_spec_directory, protected_paths):
        raise AgentError.invalid_input(
            "runtime_edit_policy.protected_paths must recursively protect task_spec.directory"
        )
    if not _directory_is_recursively_protected(state_directory, protected_paths):
        raise AgentError.invalid_input(
            "runtime_edit_policy.protected_paths must recursively protect state.directory"
        )


def _directory_is_recursively_protected(directory: str, patterns: tuple[str, ...]) -> bool:
    from agent.scope import path_matches

    rel = directory
    if not rel or rel.startswith("/") or _is_windows_drive_path(rel) or _is_unc_path(rel):
        return False
    child = f"{rel}/__runtime_policy_child__"
    return any(path_matches(rel, pattern) for pattern in patterns) and any(
        path_matches(child, pattern) for pattern in patterns
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
