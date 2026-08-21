"""Agent workflow labels. Missing labels are created (issues/pull-requests write).

Exclusive status labels: agent:review, agent:ready, agent:escalated, agent:failed.
Applying one removes the others.

Phase 7 enabled:
- deliver applies agent:review (PR created, waiting for CodeRabbit)
- review workflow applies agent:ready only after current-HEAD review converges
agent:running is not applied (execute has no GitHub write).
"""

from __future__ import annotations

from agent.github_api import GitHubClient

AGENT_LABELS: dict[str, tuple[str, str]] = {
    "agent:running": ("0E8A16", "Orchestrator is executing this work unit"),
    "agent:review": ("1D76DB", "Waiting for CodeRabbit review"),
    "agent:ready": ("5319E7", "Review converged; ready for human merge"),
    "agent:escalated": ("B60205", "Human decision required"),
    "agent:failed": ("D93F0B", "Retryable workflow failure"),
}

AGENT_STATUS_LABELS = tuple(AGENT_LABELS)
PHASE6_APPLIED_LABELS = ("agent:ready", "agent:escalated", "agent:failed")
PHASE7_APPLIED_LABELS = ("agent:review", "agent:ready", "agent:escalated", "agent:failed")
EXCLUSIVE_STATUS_LABELS = ("agent:review", "agent:ready", "agent:escalated", "agent:failed")
TERMINAL_STATUS_LABELS = ("agent:failed", "agent:escalated", "agent:ready")


def ensure_agent_labels(client: GitHubClient) -> None:
    _ensure_named_labels(client, PHASE6_APPLIED_LABELS)


def ensure_review_labels(client: GitHubClient) -> None:
    _ensure_named_labels(client, PHASE7_APPLIED_LABELS)


def current_terminal_status_label(client: GitHubClient, issue_number: int) -> str | None:
    """Return the sticky terminal label on the issue, if any.

    Prefer failed over escalated over ready when exclusive labels were stacked.
    """
    names: set[str] = set()
    for item in client.list_issue_labels(issue_number):
        name = item.get("name")
        if isinstance(name, str) and name in TERMINAL_STATUS_LABELS:
            names.add(name)
    for label in TERMINAL_STATUS_LABELS:
        if label in names:
            return label
    return None


def apply_status_label(client: GitHubClient, issue_number: int, status_label: str) -> None:
    if status_label not in AGENT_LABELS:
        raise ValueError(f"unknown agent label: {status_label}")
    if status_label in EXCLUSIVE_STATUS_LABELS:
        ensure_review_labels(client)
    else:
        ensure_agent_labels(client)
    client.add_issue_labels(issue_number, [status_label])
    if status_label in EXCLUSIVE_STATUS_LABELS:
        for name in EXCLUSIVE_STATUS_LABELS:
            if name != status_label:
                client.remove_issue_label(issue_number, name)


def _ensure_named_labels(client: GitHubClient, names: tuple[str, ...]) -> None:
    for name in names:
        color, description = AGENT_LABELS[name]
        if client.get_label(name) is None:
            client.create_label(name=name, color=color, description=description)
