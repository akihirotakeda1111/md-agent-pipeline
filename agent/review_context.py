"""Pure Task Spec semantic context for review Classifier and Repair prompts."""

from __future__ import annotations

from agent.spec import SpecTask, TaskSpec


def format_review_task_context(spec: TaskSpec) -> str:
    """Format the full Task Spec semantic context in spec order.

    This formatter does not truncate tasks, acceptance criteria, validation,
    or final verification. Callers must not shrink the result and retry.
    """
    sections = [
        "# Task Spec",
        f"- id: {spec.id}",
        f"- title: {spec.title}",
        "",
        "# Objective",
        spec.objective.strip() or "(none)",
        "",
        "# Non-Goals",
        spec.non_goals.strip() or "(none)",
        "",
        "# Architecture Invariants",
        spec.architecture_invariants.strip() or "(none)",
        "",
        "# Forbidden Actions",
        spec.forbidden_actions.strip() or "(none)",
        "",
        "# Tasks",
    ]
    if spec.tasks:
        for index, task in enumerate(spec.tasks):
            if index:
                sections.append("")
            sections.extend(_format_task(task))
    else:
        sections.append("(none)")
    sections.extend(
        [
            "",
            "# Final Verification",
            spec.final_verification.strip() or "(none)",
        ]
    )
    return "\n".join(sections)


def _format_task(task: SpecTask) -> list[str]:
    heading = f"## {task.id}: {task.title}" if task.title.strip() else f"## {task.id}"
    depends = ", ".join(task.depends_on) or "(none)"
    return [
        heading,
        f"- id: {task.id}",
        f"- title: {task.title.strip() or '(none)'}",
        f"- depends_on: {depends}",
        "",
        "### Requirement",
        task.requirement.strip() or "(none)",
        "",
        "### Acceptance Criteria",
        task.acceptance_criteria.strip() or "(none)",
        "",
        "### Validation",
        task.validation.strip() or "(none)",
    ]
