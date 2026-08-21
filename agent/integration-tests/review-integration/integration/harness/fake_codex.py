from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .observations import ObservationLog


@dataclass(frozen=True)
class CodexStep:
    changes: dict[str, str] = field(default_factory=dict)
    exit_code: int = 0
    final_message: str = "scripted review repair completed"


class ScriptedCodex:
    """Consumes scripted responses and records invocations without decisions."""

    def __init__(self, steps: list[CodexStep], observations: ObservationLog) -> None:
        self._steps = list(steps)
        self.observations = observations
        self.invocations: list[dict[str, Any]] = []

    def invoke(
        self,
        *,
        workspace: Path,
        prompt: str,
        env: dict[str, str],
        argv: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self._steps:
            raise AssertionError("Fake Codex invoked more times than scripted")
        step = self._steps.pop(0)
        command = argv or ["fake-codex"]
        self.observations.process("codex", command, env)
        self.invocations.append({"prompt": prompt, "env_keys": sorted(env), "argv": list(command)})
        for relative, content in step.changes.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return {"exit_code": step.exit_code, "final_message": step.final_message}
