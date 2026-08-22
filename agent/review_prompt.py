"""Prompt construction for Codex review repair. Classifier does not use this."""

from __future__ import annotations

from pathlib import Path

from agent.config import RuntimeEditPolicy
from agent.gitutil import working_tree_diff_text
from agent.review_context import format_review_task_context
from agent.review_types import ClassificationResult, ReviewFeedback
from agent.scope import format_scope_prompt_sections
from agent.spec import TaskSpec

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "review-repair.md"


def build_review_repair_prompt(
    spec: TaskSpec,
    *,
    repo_root: Path | str,
    base_sha: str,
    accepted: tuple[tuple[ReviewFeedback, ClassificationResult], ...],
    runtime_policy: RuntimeEditPolicy,
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
    diff = working_tree_diff_text(repo_root, base_sha)
    return "\n".join(
        [
            contract,
            "",
            format_scope_prompt_sections(spec, runtime_policy),
            "",
            format_review_task_context(spec),
            "",
            "# Accepted review comments",
            "\n\n".join(comments) or "(none)",
            "",
            "# Current diff versus delivery base",
            diff or "(no diff)",
            "",
            "Protected paths cannot be edited even when listed in Allowed Paths.",
            "",
        ]
    )
