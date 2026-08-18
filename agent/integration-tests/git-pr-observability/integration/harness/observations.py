from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProcessObservation:
    role: str
    argv: tuple[str, ...]
    env_keys: frozenset[str]


@dataclass(frozen=True)
class GitHubObservation:
    operation: str
    payload: dict[str, Any]


@dataclass
class ObservationLog:
    timeline: list[str] = field(default_factory=list)
    processes: list[ProcessObservation] = field(default_factory=list)
    github: list[GitHubObservation] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def mark(self, name: str) -> None:
        self.timeline.append(name)

    def process(self, role: str, argv: list[str] | tuple[str, ...], env: dict[str, str]) -> None:
        self.processes.append(ProcessObservation(role, tuple(argv), frozenset(env)))
        self.timeline.append(f"process:{role}")

    def github_call(self, operation: str, payload: dict[str, Any]) -> None:
        self.github.append(GitHubObservation(operation, dict(payload)))
        self.timeline.append(f"github:{operation}")

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append(dict(event))
        self.timeline.append(f"event:{event.get('event', event.get('type', 'UNKNOWN'))}")

    def write_jsonl(self, path: Path) -> None:
        path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in self.events), encoding="utf-8")


def event_names(events: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("event", item.get("type", ""))) for item in events]


def assert_in_order(values: list[str], required: list[str]) -> None:
    cursor = -1
    for value in required:
        cursor = values.index(value, cursor + 1)
