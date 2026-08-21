"""Repair prompt construction and attempt-limit policy."""

from __future__ import annotations

from pathlib import Path

from agent.config import RuntimeEditPolicy
from agent.errors import AgentError
from agent.scope import format_scope_prompt_sections
from agent.spec import SpecTask, TaskSpec
from agent.validation import ValidationRecord

REPAIR_INSTRUCTION_PATH = Path(__file__).resolve().parent / "prompts" / "repair.md"


def load_repair_instruction() -> str:
    try:
        return REPAIR_INSTRUCTION_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AgentError.environment_failure(
            f"repair prompt not found: {REPAIR_INSTRUCTION_PATH}"
        ) from exc


def remaining_repairs(limit: int, used: int) -> int:
    return max(0, limit - used)


def can_attempt_repair(limit: int, used: int) -> bool:
    return remaining_repairs(limit, used) > 0


def build_repair_prompt(
    spec: TaskSpec,
    task: SpecTask,
    *,
    repo_root: Path | str,
    failed: ValidationRecord,
    diff_text: str,
    runtime_policy: RuntimeEditPolicy,
) -> str:
    instruction = load_repair_instruction()
    error_output = (failed.stderr or failed.stdout).strip()
    if len(error_output) > 4000:
        error_output = error_output[-4000:]
    return (
        "\n".join(
            [
                instruction,
                "",
                "# Repository",
                f"- path: {Path(repo_root)}",
                "",
                format_scope_prompt_sections(spec, runtime_policy),
                "",
                "# Architecture Invariants",
                spec.architecture_invariants.strip(),
                "",
                "# Current Task",
                f"- id: {task.id}",
                f"- title: {task.title}",
                "",
                "## Requirement",
                task.requirement.strip(),
                "",
                "## Acceptance Criteria",
                task.acceptance_criteria.strip(),
                "",
                "# Failed Validation",
                f"- command: {failed.command}",
                f"- exit_code: {failed.exit_code}",
                f"- timed_out: {failed.timed_out}",
                "",
                "## Error output",
                error_output or "(empty)",
                "",
                "# Current Diff",
                diff_text.strip() or "(no diff)",
                "",
                "Do not delete tests or disable lint to make validation pass.",
                "Protected paths cannot be edited even when listed in Allowed Paths.",
            ]
        )
        + "\n"
    )
