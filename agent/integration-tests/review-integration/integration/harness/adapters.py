from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .fake_classifier import ScriptedClassifier
from .fake_codex import ScriptedCodex
from .fake_github import FakeGitHub
from .git_repo import GitRepo, RecordingProcessRunner
from .observations import ObservationLog


@dataclass(frozen=True)
class ReviewEvent:
    event_name: str
    actor: str
    pr_number: int
    head_sha: str
    object_id: str


@dataclass(frozen=True)
class ReviewRunRequest:
    event: ReviewEvent
    spec_path: Path
    repo_root: Path
    work_unit_id: str
    expected_pr_number: int
    expected_head_sha: str
    review_attempts: int = 0
    environment: dict[str, str] = field(default_factory=dict)
    auto_repair_enabled: bool | None = None


@dataclass
class ReviewRunResult:
    status: str
    reason: str | None = None
    review_attempts: int = 0
    processed_identities: list[str] = field(default_factory=list)
    pending_identities: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class ServiceBundle:
    github: FakeGitHub
    classifier: ScriptedClassifier
    codex: ScriptedCodex
    git_repo: GitRepo
    processes: RecordingProcessRunner
    observations: ObservationLog


@runtime_checkable
class Phase7Driver(Protocol):
    """Stable test-facing boundary translated by one Production binding.

    Implementations must delegate all decisions to Production components. A
    binding that contains review policy, scope, validation, retry, convergence,
    or Git business logic does not satisfy this contract.
    """

    def run_review(self, request: ReviewRunRequest, services: ServiceBundle) -> ReviewRunResult: ...


def require_status(result: ReviewRunResult, expected: str) -> None:
    assert result.status.upper() == expected, (
        f"expected {expected}, got {result.status}: {result.reason}"
    )
