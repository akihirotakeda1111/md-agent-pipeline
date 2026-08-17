"""Human notification payload for ESCALATED / FAILED outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from agent.config import AgentConfig, load_config


@dataclass(frozen=True)
class EscalationNotice:
    task_id: str
    current_task: str | None
    reason: str
    last_validation: str | None
    repair_attempts: int
    required_human_action: str
    mention: str | None = None

    def to_markdown(self) -> str:
        mention_line = (
            f"@{self.mention.lstrip('@')}" if self.mention else "No mention target is configured."
        )
        current = self.current_task or "none"
        last = self.last_validation or "none"
        return "\n".join(
            [
                "## Agent escalation",
                "",
                f"- Task ID: `{self.task_id}`",
                f"- Current Task: `{current}`",
                f"- Reason: {self.reason}",
                f"- Last Validation: `{last}`",
                f"- Repair Attempts: {self.repair_attempts}",
                f"- Required Human Action: {self.required_human_action}",
                f"- Mention: {mention_line}",
                "",
            ]
        )

    def to_json_dict(self) -> dict[str, str | int | None]:
        return {
            "task_id": self.task_id,
            "current_task": self.current_task,
            "reason": self.reason,
            "last_validation": self.last_validation,
            "repair_attempts": self.repair_attempts,
            "required_human_action": self.required_human_action,
            "mention": self.mention,
        }


def mention_from_config(config: AgentConfig | None = None) -> str | None:
    cfg = config or load_config()
    mention = cfg.notification.mention
    if mention is None or not mention.strip():
        return None
    return mention.strip().lstrip("@")
