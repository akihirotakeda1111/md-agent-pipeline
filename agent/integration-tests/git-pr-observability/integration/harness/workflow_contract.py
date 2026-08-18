from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_workflow(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict) or not isinstance(loaded.get("jobs"), dict):
        raise AssertionError(f"Invalid workflow structure: {path}")
    return loaded


def effective_permissions(workflow: dict[str, Any], job_name: str) -> dict[str, str]:
    jobs = workflow["jobs"]
    job = jobs[job_name]
    root = workflow.get("permissions") or {}
    local = job.get("permissions")
    if not isinstance(root, dict):
        raise AssertionError("Workflow permissions must be an explicit mapping")
    if local is None:
        return {str(key): str(value) for key, value in root.items()}
    if not isinstance(local, dict):
        raise AssertionError(f"Job {job_name} permissions must be an explicit mapping")
    return {str(key): str(value) for key, value in local.items()}


def scalar_text(value: Any) -> str:
    if isinstance(value, dict):
        return "\n".join(f"{key}\n{scalar_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return "\n".join(scalar_text(item) for item in value)
    return str(value)


def contains_reference(value: Any, name: str) -> bool:
    return name.lower() in scalar_text(value).lower()


def checkout_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in job.get("steps", [])
        if isinstance(step, dict) and str(step.get("uses", "")).lower().startswith("actions/checkout@")
    ]


def codex_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict) and contains_reference(step, "codex")]


def is_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"


def is_zero(value: Any) -> bool:
    return value == 0 or str(value).strip() == "0"
