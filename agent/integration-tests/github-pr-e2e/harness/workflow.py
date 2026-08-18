from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import ProductionGap


def load_workflow(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"invalid workflow YAML: {path}")
    return value


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    value = workflow.get("on", workflow.get(True))
    if isinstance(value, str):
        return {value: {}}
    if isinstance(value, list):
        return {str(item): {} for item in value}
    if isinstance(value, dict):
        return {str(key): (item or {}) for key, item in value.items()}
    return {}


def _glob_regex(pattern: str) -> re.Pattern[str]:
    output = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*" and index + 1 < len(pattern) and pattern[index + 1] == "*":
            output += ".*"
            index += 2
            if index < len(pattern) and pattern[index] == "/":
                output += "/?"
                index += 1
            continue
        if char == "*":
            output += "[^/]*"
        elif char == "?":
            output += "[^/]"
        else:
            output += re.escape(char)
        index += 1
    return re.compile(f"^{output}$")


def _patterns(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def matches_filters(value: str, includes: Any, excludes: Any) -> bool:
    includes = _patterns(includes)
    excludes = _patterns(excludes)
    if includes:
        selected = False
        for raw in includes:
            negative = raw.startswith("!")
            pattern = raw[1:] if negative else raw
            if _glob_regex(pattern).match(value):
                selected = not negative
        if not selected:
            return False
    if excludes and any(_glob_regex(pattern).match(value) for pattern in excludes):
        return False
    return True


def push_eligible(push: Any, branch: str, changed_path: str) -> bool:
    config = push if isinstance(push, dict) else {}
    return matches_filters(
        branch, config.get("branches"), config.get("branches-ignore")
    ) and matches_filters(changed_path, config.get("paths"), config.get("paths-ignore"))


def choose_trigger(
    workflow: dict[str, Any], requested: str, branch: str, changed_path: str
) -> tuple[str, dict[str, Any]]:
    available = triggers(workflow)
    push_ok = "push" in available and push_eligible(available["push"], branch, changed_path)
    dispatch = available.get("workflow_dispatch")
    if requested == "push":
        if not push_ok:
            raise ProductionGap(
                "E2E_SAFE_PUSH_TRIGGER_UNAVAILABLE",
                contract=(
                    "Use the existing Production trigger without committing to the default branch"
                ),
                observed=(
                    "Production push filters do not accept the isolated E2E source branch/spec path"
                ),
                impact="The E2E cannot safely start the Production workflow by push",
                required_change=(
                    "Decide whether an existing workflow_dispatch interface "
                    "is the supported E2E entry; do not add an E2E-only trigger"
                ),
            )
        return "push", {}
    if requested == "workflow_dispatch":
        if dispatch is None:
            raise ProductionGap(
                "E2E_WORKFLOW_DISPATCH_UNAVAILABLE",
                contract="Use only an existing Production workflow_dispatch interface",
                observed="Production workflow has no workflow_dispatch trigger",
                impact="The requested dispatch trigger cannot be used",
                required_change=(
                    "Use a supported existing trigger or explicitly define a "
                    "Production dispatch contract outside this E2E change"
                ),
            )
        return "workflow_dispatch", dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
    if push_ok:
        return "push", {}
    if dispatch is not None:
        return "workflow_dispatch", dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
    raise ProductionGap(
        "E2E_SAFE_TRIGGER_UNAVAILABLE",
        contract="An existing Production trigger must safely accept an isolated source branch",
        observed="Neither eligible push nor workflow_dispatch is available",
        impact="The Real GitHub E2E cannot start without changing Production",
        required_change=(
            "Define a safe Production trigger as a separate Production change; "
            "do not create a shadow workflow"
        ),
    )


def resolve_dispatch_inputs(
    definitions: dict[str, Any], supplied: dict[str, str], *, spec_path: str, task_id: str
) -> dict[str, str]:
    unknown = sorted(set(supplied) - set(definitions))
    if unknown:
        raise ValueError(f"dispatch input(s) not defined by Production workflow: {unknown}")
    values = dict(supplied)
    for name, definition in definitions.items():
        config = definition if isinstance(definition, dict) else {}
        if name in values:
            continue
        normalized = name.lower().replace("-", "_")
        if normalized in {"spec", "spec_path", "task_spec", "task_spec_path"}:
            values[name] = spec_path
        elif normalized in {"task", "task_id", "work_unit", "work_unit_id"}:
            values[name] = task_id
        elif "default" in config:
            values[name] = str(config["default"])
        elif config.get("required"):
            raise RuntimeError(f"required workflow_dispatch input needs --dispatch-input: {name}")
    return values
