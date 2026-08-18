from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .observations import ObservationLog


@dataclass(frozen=True)
class CodexStep:
    changes: dict[str, str] = field(default_factory=dict)
    exit_code: int = 0
    final_message: str = "test double completed"


class ScriptedCodex:
    """A dumb subprocess substitute: consume one scripted response and record it.

    Callers identify repair vs implementation from invocation count and workspace
    state, not from Fake-owned stage labels.
    """

    def __init__(self, steps: list[CodexStep], observations: ObservationLog) -> None:
        self._steps = list(steps)
        self.observations = observations
        self.invocations: list[dict[str, object]] = []

    def invoke(self, *, workspace: Path, env: dict[str, str], argv: list[str] | None = None) -> dict[str, object]:
        if not self._steps:
            raise AssertionError("Fake Codex invoked more times than scripted")
        step = self._steps.pop(0)
        recorded_argv = argv or ["fake-codex"]
        self.observations.process("codex", recorded_argv, env)
        self.invocations.append({"env_keys": sorted(env), "argv": list(recorded_argv)})
        for relative, content in step.changes.items():
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return {"exit_code": step.exit_code, "final_message": step.final_message}


def success_steps(*changes: dict[str, str]) -> list[CodexStep]:
    return [CodexStep(change) for change in changes]
