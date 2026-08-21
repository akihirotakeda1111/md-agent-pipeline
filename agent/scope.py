"""Mechanical scope check against actual Git changes."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

from agent.config import RuntimeEditPolicy
from agent.errors import AgentError
from agent.gitutil import GitChange, normalize_git_path
from agent.spec import TaskSpec


@dataclass(frozen=True)
class ScopeCheckResult:
    allowed: bool
    changed_paths: tuple[str, ...]
    violation_paths: tuple[str, ...]
    reason: str | None = None

    def to_json_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "changed_paths": list(self.changed_paths),
            "violation_paths": list(self.violation_paths),
            "reason": self.reason,
        }


def normalize_pattern(pattern: str) -> str:
    return normalize_git_path(pattern)


def path_matches(path: str, pattern: str) -> bool:
    path = normalize_git_path(path)
    pattern = normalize_pattern(pattern)
    if not path or not pattern:
        return False
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    if "/" not in pattern and "*" not in pattern and "?" not in pattern:
        return path == pattern or path.startswith(pattern + "/")
    return fnmatch.fnmatch(path, pattern)


def path_is_protected(path: str, runtime_policy: RuntimeEditPolicy) -> bool:
    return any(path_matches(path, pattern) for pattern in runtime_policy.protected_paths)


def path_is_in_scope(path: str, spec: TaskSpec, runtime_policy: RuntimeEditPolicy) -> bool:
    """Protected > Forbidden > Allowed. Unmatched paths are default deny."""
    if path_is_protected(path, runtime_policy):
        return False
    if any(path_matches(path, pattern) for pattern in spec.forbidden_paths):
        return False
    return any(path_matches(path, pattern) for pattern in spec.allowed_paths)


def check_scope(
    spec: TaskSpec,
    changes: tuple[GitChange, ...] | list[GitChange],
    runtime_policy: RuntimeEditPolicy,
) -> ScopeCheckResult:
    changed: list[str] = []
    violations: list[str] = []
    for change in changes:
        for path in change.paths:
            if path not in changed:
                changed.append(path)
            if not path_is_in_scope(path, spec, runtime_policy) and path not in violations:
                violations.append(path)
    if violations:
        return ScopeCheckResult(
            allowed=False,
            changed_paths=tuple(changed),
            violation_paths=tuple(violations),
            reason="SCOPE_VIOLATION",
        )
    return ScopeCheckResult(allowed=True, changed_paths=tuple(changed), violation_paths=())


def validate_spec_scope_policy(spec: TaskSpec, runtime_policy: RuntimeEditPolicy) -> None:
    """Fail closed when Task Spec allowed_paths clearly overlap Repository protection.

    This is Defense in Depth, not a complete glob-intersection engine.
    """
    protected = runtime_policy.protected_paths
    for allowed in spec.allowed_paths:
        pattern = normalize_pattern(allowed)
        if not pattern:
            continue
        if pattern == "**":
            raise AgentError.invalid_spec(
                "allowed_paths may not include '**'; Repository Runtime Edit Policy "
                "cannot be widened by a Task Spec"
            )
        if any(pattern == normalize_pattern(item) for item in protected):
            raise AgentError.invalid_spec(
                f"allowed_paths {allowed!r} duplicates a Repository protected path"
            )
        if _is_exact_path_pattern(pattern) and any(
            path_matches(pattern, item) for item in protected
        ):
            raise AgentError.invalid_spec(
                f"allowed_paths {allowed!r} is covered by Repository protected paths"
            )


def format_scope_prompt_sections(spec: TaskSpec, runtime_policy: RuntimeEditPolicy) -> str:
    forbidden = spec.forbidden_paths or ("(none listed)",)
    lines = [
        "# Repository Protected Paths",
        "Repository Protected Paths = absolute deny regions that a Task Spec cannot "
        "remove, override, or temporarily disable.",
        *[f"- {item}" for item in runtime_policy.protected_paths],
        "",
        "# Task-level Forbidden Paths",
        "Task-level Forbidden Paths = additional deny regions for this task only.",
        *[f"- {item}" for item in forbidden],
        "",
        "# Allowed Paths",
        "Allowed Paths = the maximum editable range, and only when the change is "
        "required for the current task and the path is not Protected or Forbidden.",
        *[f"- {item}" for item in spec.allowed_paths],
        "",
        "Anything else = Default Deny.",
        "Conflict priority: Protected > Forbidden > Allowed.",
    ]
    return "\n".join(lines)


def _is_exact_path_pattern(pattern: str) -> bool:
    return "*" not in pattern and "?" not in pattern and "[" not in pattern
