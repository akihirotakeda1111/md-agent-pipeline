"""Prompt construction for Codex review repair. Classifier does not use this."""

from __future__ import annotations

from pathlib import Path

from agent.gitutil import working_tree_diff_text
from agent.review_types import ClassificationResult, ReviewFeedback
from agent.spec import SpecTask, TaskSpec

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review-repair.md"


def build_review_repair_prompt(
    spec: TaskSpec,
    *,
    repo_root: Path | str,
    base_sha: str,
    accepted: tuple[tuple[ReviewFeedback, ClassificationResult], ...],
    current_task: SpecTask | None,
) -> str:
    contract = PROMPT_PATH.read_text(encoding="utf-8").strip()
    comments = []
    for item, result in accepted:
        paths = ", ".join(result.referenced_paths) or item.path or "(none)"
        comments.append(
            "\n".join(
                [
                    f"### {item.identity}",
                    f"- classification: {result.classification.value}",
                    f"- confidence: {result.confidence}",
                    f"- referencedPaths: {paths}",
                    f"- reason: {result.reason}",
                    "",
                    item.body.strip(),
                ]
            )
        )
    task_block = "(no current task)"
    if current_task is not None:
        task_block = "\n".join(
            [
                f"id: {current_task.id}",
                f"title: {current_task.title}",
                "",
                "## Requirement",
                current_task.requirement.strip() or "(none)",
                "",
                "## Acceptance Criteria",
                current_task.acceptance_criteria.strip() or "(none)",
                "",
                "## Validation",
                current_task.validation.strip() or "(none)",
            ]
        )
    diff = working_tree_diff_text(repo_root, base_sha)
    return "\n".join(
        [
            contract,
            "",
            "# Task Spec",
            f"- id: {spec.id}",
            f"- title: {spec.title}",
            f"- allowed_paths: {', '.join(spec.allowed_paths)}",
            f"- forbidden_paths: {', '.join(spec.forbidden_paths)}",
            "",
            "# Objective",
            spec.objective.strip() or "(none)",
            "",
            "# Architecture Invariants",
            spec.architecture_invariants.strip() or "(none)",
            "",
            "# Forbidden Actions",
            spec.forbidden_actions.strip() or "(none)",
            "",
            "# Current Task",
            task_block,
            "",
            "# Accepted review comments",
            "\n\n".join(comments) or "(none)",
            "",
            "# Current diff versus delivery base",
            diff or "(no diff)",
            "",
            "# Final Verification",
            spec.final_verification.strip() or "(none)",
            "",
        ]
    )
