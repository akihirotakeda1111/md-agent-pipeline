"""GitHub Actions job summary rendering.

Official: append Markdown to the file named by GITHUB_STEP_SUMMARY.
https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-commands#adding-a-job-summary
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def render_summary(
    *,
    spec_path: str,
    task_id: str,
    state: str,
    current_task: str | None,
    completed_tasks: Sequence[str],
    changed_files: Sequence[str],
    validation_results: Sequence[str],
    repair_attempts: int,
    pr_url: str | None,
    failure_reason: str | None,
    escalation_reason: str | None,
) -> str:
    def bullets(items: Sequence[str]) -> str:
        return "\n".join(f"- `{item}`" for item in items) or "- None"

    return "\n".join(
        [
            "# Agent Execute",
            "",
            f"- Task Spec: `{spec_path}`",
            f"- Task ID: `{task_id}`",
            f"- State: `{state}`",
            f"- Current Task: `{current_task or 'none'}`",
            f"- Repair Attempts: {repair_attempts}",
            f"- PR URL: {pr_url or 'none'}",
            f"- Failure Reason: {failure_reason or 'none'}",
            f"- Escalation Reason: {escalation_reason or 'none'}",
            "",
            "## Completed Tasks",
            bullets(completed_tasks),
            "",
            "## Changed Files",
            bullets(changed_files),
            "",
            "## Validation Results",
            "\n".join(f"- {item}" for item in validation_results) or "- None",
            "",
        ]
    )


def write_github_summary(path: Path | str, markdown: str) -> None:
    summary_path = Path(path)
    with summary_path.open("a", encoding="utf-8") as handle:
        handle.write(markdown)
        if not markdown.endswith("\n"):
            handle.write("\n")
