from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import ProductionBug


def load_workflow(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProductionBug(f"cannot parse Production workflow: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProductionBug(f"invalid workflow YAML: {path}")
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


def choose_execute_trigger(
    workflow: dict[str, Any], requested: str, branch: str, changed_path: str
) -> tuple[str, dict[str, Any]]:
    available = triggers(workflow)
    push_ok = "push" in available and push_eligible(available["push"], branch, changed_path)
    dispatch = available.get("workflow_dispatch")
    if requested == "push":
        if not push_ok:
            raise ProductionBug(
                "Production push filters do not accept the isolated Phase 7 E2E branch/spec path"
            )
        return "push", {}
    if requested == "workflow_dispatch":
        if dispatch is None:
            raise ProductionBug("Production execute workflow has no workflow_dispatch trigger")
        return "workflow_dispatch", dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
    if push_ok:
        return "push", {}
    if dispatch is not None:
        return "workflow_dispatch", dispatch.get("inputs", {}) if isinstance(dispatch, dict) else {}
    raise ProductionBug("no safe existing Production execute trigger accepts the E2E work unit")


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


COMMENT_REVIEW_EVENTS = frozenset(
    {"pull_request_review", "pull_request_review_comment", "issue_comment"}
)
TERMINAL_REVIEW_EVENTS = frozenset({"check_run", "status"})
SUPPORTED_REVIEW_EVENTS = TERMINAL_REVIEW_EVENTS


def review_event_names(workflow: dict[str, Any]) -> tuple[str, ...]:
    available = triggers(workflow)
    return tuple(name for name in ("check_run", "status") if name in available)


def assert_review_workflow_contract(workflow: dict[str, Any]) -> tuple[str, ...]:
    available = set(triggers(workflow))
    comment_events = sorted(available & COMMENT_REVIEW_EVENTS)
    if comment_events:
        raise ProductionBug(
            "agent-review.yml must not start from comment events: " + ", ".join(comment_events)
        )
    events = review_event_names(workflow)
    missing_terminal = sorted(TERMINAL_REVIEW_EVENTS - set(events))
    if missing_terminal:
        raise ProductionBug(
            "agent-review.yml missing CodeRabbit terminal wake-up event(s): "
            f"{missing_terminal}"
        )
    text = str(workflow).lower()
    if "pull_request_target" in text:
        raise ProductionBug("agent-review.yml uses forbidden pull_request_target")
    if "sleep " in text or "while true" in text:
        raise ProductionBug("Production review workflow contains unbounded/fixed waiting logic")
    if workflow.get("concurrency") not in (None, {}):
        raise ProductionBug("agent-review.yml must not use workflow-level concurrency")
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        raise ProductionBug("agent-review.yml has no jobs")
    prepare = jobs.get("prepare")
    if isinstance(prepare, dict) and prepare.get("concurrency") not in (None, {}):
        raise ProductionBug("prepare job must not take a concurrency lock")
    review = jobs.get("review")
    if not isinstance(review, dict):
        raise ProductionBug("agent-review.yml is missing the review job")
    concurrency = review.get("concurrency")
    if not isinstance(concurrency, dict):
        raise ProductionBug("review job must serialize same-PR work with a concurrency group")
    group = str(concurrency.get("group") or "")
    if "needs.prepare.outputs.pull_number" not in group:
        raise ProductionBug("review concurrency group must use prepare's API pull_number")
    if "github.workflow" not in group:
        raise ProductionBug("review concurrency group must prefix with github.workflow")
    if concurrency.get("cancel-in-progress") not in (False, "false"):
        raise ProductionBug("review job must serialize same-PR events without cancellation")
    return events
