from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_workflow(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), dict):
        raise AssertionError(f"Invalid workflow structure: {path}")
    return value


def scalar_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}\n{scalar_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(scalar_text(item) for item in value)
    return str(value)


def contains_reference(value: Any, name: str) -> bool:
    return name.lower() in scalar_text(value).lower()


def effective_permissions(workflow: dict[str, Any], job_name: str) -> dict[str, str]:
    root = workflow.get("permissions") or {}
    local = workflow["jobs"][job_name].get("permissions")
    value = root if local is None else local
    if not isinstance(value, dict):
        raise AssertionError("Workflow permissions must be an explicit mapping")
    return {str(key): str(item) for key, item in value.items()}


def checkout_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
        and str(step.get("uses", "")).lower().startswith("actions/checkout@")
    ]


def is_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"
