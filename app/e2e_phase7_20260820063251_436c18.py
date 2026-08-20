"""Isolated CSV normalizer for phase 7 E2E."""

from __future__ import annotations


def normalize_csv(value: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in value.split(","):
        normalized = item.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)
