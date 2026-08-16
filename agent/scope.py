"""Mechanical scope check against actual Git changes."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass

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


def path_is_in_scope(path: str, spec: TaskSpec) -> bool:
    """Forbidden wins. Paths matching no allow pattern are out of scope."""
    if any(path_matches(path, pattern) for pattern in spec.forbidden_paths):
        return False
    return any(path_matches(path, pattern) for pattern in spec.allowed_paths)


def check_scope(
    spec: TaskSpec, changes: tuple[GitChange, ...] | list[GitChange]
) -> ScopeCheckResult:
    changed: list[str] = []
    violations: list[str] = []
    for change in changes:
        for path in change.paths:
            if path not in changed:
                changed.append(path)
            if not path_is_in_scope(path, spec) and path not in violations:
                violations.append(path)
    if violations:
        return ScopeCheckResult(
            allowed=False,
            changed_paths=tuple(changed),
            violation_paths=tuple(violations),
            reason="SCOPE_VIOLATION",
        )
    return ScopeCheckResult(allowed=True, changed_paths=tuple(changed), violation_paths=())
