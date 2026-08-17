"""Pull Request body generation. Does not call GitHub."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from agent.spec import TaskSpec

WORK_UNIT_MARKER_START = "<!-- md-agent-work-unit"
WORK_UNIT_MARKER_END = "-->"
_MARKER_FIELDS = ("spec_id", "base_branch", "target_branch")


def build_pr_title(spec: TaskSpec) -> str:
    return f"{spec.id}: {spec.title}"


def build_work_unit_marker(spec: TaskSpec) -> str:
    return "\n".join(
        [
            WORK_UNIT_MARKER_START,
            f"spec_id: {spec.id}",
            f"base_branch: {spec.base_branch}",
            f"target_branch: {spec.target_branch}",
            WORK_UNIT_MARKER_END,
        ]
    )


def parse_work_unit_marker(body: str | None) -> dict[str, str] | None:
    if not body:
        return None
    start = body.find(WORK_UNIT_MARKER_START)
    if start < 0:
        return None
    end = body.find(WORK_UNIT_MARKER_END, start + len(WORK_UNIT_MARKER_START))
    if end < 0:
        return None
    block = body[start + len(WORK_UNIT_MARKER_START) : end]
    fields: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in _MARKER_FIELDS and value:
            if key in fields:
                return None
            fields[key] = value
    if any(key not in fields for key in _MARKER_FIELDS):
        return None
    return fields


def _pull_ref(pull: Mapping[str, Any], side: str) -> str | None:
    node = pull.get(side)
    if not isinstance(node, dict):
        return None
    ref = node.get("ref")
    if not isinstance(ref, str) or not ref.strip():
        return None
    return ref


def is_same_work_unit_pull(spec: TaskSpec, pull: Mapping[str, Any]) -> bool:
    """True only when GitHub refs and the PR marker identify this work unit."""
    marker = parse_work_unit_marker(str(pull.get("body") or ""))
    if marker is None:
        return False
    head = _pull_ref(pull, "head")
    base = _pull_ref(pull, "base")
    return (
        marker["spec_id"] == spec.id
        and marker["base_branch"] == spec.base_branch
        and marker["target_branch"] == spec.target_branch
        and head == spec.target_branch
        and base == spec.base_branch
    )


def build_pr_body(
    spec: TaskSpec,
    *,
    completed_tasks: Sequence[str],
    changed_files: Sequence[str],
    validation_results: Sequence[str],
    final_verification: str,
    repair_attempts: int,
    known_limitations: str = "None recorded.",
    human_review_points: str | None = None,
    escalation_history: Sequence[str] | None = None,
) -> str:
    tasks = "\n".join(f"- `{task_id}`" for task_id in completed_tasks) or "- None"
    files = "\n".join(f"- `{path}`" for path in changed_files) or "- None"
    validations = "\n".join(f"- {item}" for item in validation_results) or "- None"
    review = (
        human_review_points
        or spec.forbidden_actions.strip()
        or "Review allowed_paths and Validation."
    )
    history = "\n".join(f"- {item}" for item in (escalation_history or ())) or "- None"
    return "\n".join(
        [
            build_work_unit_marker(spec),
            "",
            "## Task Spec",
            f"`{spec.source_path or spec.id}` (`{spec.id}`)",
            "",
            "## Objective",
            spec.objective.strip() or "See Task Spec.",
            "",
            "## Completed Tasks",
            tasks,
            "",
            "## Changed Files",
            files,
            "",
            "## Validation Results",
            validations,
            "",
            "## Final Verification",
            final_verification.strip() or "Not run.",
            "",
            "## Repair Attempts",
            str(repair_attempts),
            "",
            "## Known Limitations",
            known_limitations.strip() or "None recorded.",
            "",
            "## Human Review Points",
            review,
            "",
            "## Escalation History",
            history,
            "",
        ]
    )
