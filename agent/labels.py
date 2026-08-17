"""Agent workflow labels. Missing labels are created (issues/pull-requests write).

Phase 6 applies only agent:ready / agent:escalated / agent:failed.
agent:running is not applied (execute has no GitHub write).
agent:review is Phase 7.
"""

from __future__ import annotations

from agent.github_api import GitHubClient

AGENT_LABELS: dict[str, tuple[str, str]] = {
    "agent:running": ("0E8A16", "Orchestrator is executing this work unit"),
    "agent:review": ("1D76DB", "Waiting for review"),
    "agent:ready": ("5319E7", "Final verification passed; ready for human review"),
    "agent:escalated": ("B60205", "Human decision required"),
    "agent:failed": ("D93F0B", "Retryable workflow failure"),
}

AGENT_STATUS_LABELS = tuple(AGENT_LABELS)
PHASE6_APPLIED_LABELS = ("agent:ready", "agent:escalated", "agent:failed")


def ensure_agent_labels(client: GitHubClient) -> None:
    for name in PHASE6_APPLIED_LABELS:
        color, description = AGENT_LABELS[name]
        if client.get_label(name) is None:
            client.create_label(name=name, color=color, description=description)


def apply_status_label(client: GitHubClient, issue_number: int, status_label: str) -> None:
    if status_label not in AGENT_LABELS:
        raise ValueError(f"unknown agent label: {status_label}")
    ensure_agent_labels(client)
    client.add_issue_labels(issue_number, [status_label])
