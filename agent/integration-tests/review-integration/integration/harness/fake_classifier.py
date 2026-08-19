from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .observations import ObservationLog


@dataclass(frozen=True)
class ClassifierStep:
    raw_output: str


class ScriptedClassifier:
    """Returns opaque scripted transport output; never parses or applies policy."""

    def __init__(self, steps: list[ClassifierStep], observations: ObservationLog) -> None:
        self._steps = list(steps)
        self.observations = observations
        self.invocations: list[dict[str, Any]] = []

    def classify(
        self, *, payload: dict[str, Any], schema: dict[str, Any], env: dict[str, str]
    ) -> str:
        if not self._steps:
            raise AssertionError("Fake classifier invoked more times than scripted")
        step = self._steps.pop(0)
        self.observations.process("classifier", ["fake-classifier"], env)
        self.invocations.append(
            {"payload": dict(payload), "schema": dict(schema), "env_keys": sorted(env)}
        )
        return step.raw_output


def classification(
    value: str,
    *,
    confidence: float = 0.95,
    paths: tuple[str, ...] = ("app/review.txt",),
) -> ClassifierStep:
    import json

    return ClassifierStep(
        json.dumps(
            {
                "classification": value,
                "confidence": confidence,
                "reason": "scripted semantic classification",
                "referencedPaths": list(paths),
            }
        )
    )
