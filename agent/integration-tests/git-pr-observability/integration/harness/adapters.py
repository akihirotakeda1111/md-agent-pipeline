from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .fake_codex import ScriptedCodex
from .fake_github import FakeGitHub
from .git_repo import GitRepo, RecordingProcessRunner
from .observations import ObservationLog


@dataclass(frozen=True)
class ArtifactBundle:
    report_path: Path
    patch_path: Path


@dataclass(frozen=True)
class WorkUnitRequest:
    spec_path: Path
    repo_root: Path
    persist_state: bool = False
    environment: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryRequest:
    spec_path: Path
    repo_root: Path
    artifacts: ArtifactBundle
    environment: dict[str, str] = field(default_factory=dict)
    notification_mention: str | None = None


@dataclass(frozen=True)
class NotificationRequest:
    status: str
    task_id: str
    current_task: str | None
    reason: str
    last_validation: str | None
    repair_attempts: int
    required_human_action: str
    pr_number: int | None = None
    mention: str | None = None


@dataclass(frozen=True)
class Phase6FlowRequest:
    """Request to the highest Production Phase 6 orchestration entry point."""

    spec_path: Path
    repo_root: Path
    execute_environment: dict[str, str] = field(default_factory=dict)
    deliver_environment: dict[str, str] = field(default_factory=dict)
    notification_mention: str | None = None


@dataclass
class WorkUnitResult:
    status: str
    reason: str | None = None
    artifacts: ArtifactBundle | None = None
    current_task: str | None = None
    completed_tasks: list[str] = field(default_factory=list)
    repair_attempts: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class DeliveryResult:
    status: str
    reason: str | None = None
    pr_url: str | None = None
    reused: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class Phase6FlowResult:
    status: str
    reason: str | None = None
    pr_url: str | None = None
    current_task: str | None = None
    completed_tasks: list[str] = field(default_factory=list)
    repair_attempts: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class ServiceBundle:
    codex: ScriptedCodex
    github: FakeGitHub
    git_repo: GitRepo
    processes: RecordingProcessRunner
    observations: ObservationLog
    validation_responses: list[bool] = field(default_factory=lambda: [True])
    final_validation_responses: list[bool] = field(default_factory=lambda: [True])


@runtime_checkable
class Phase6Driver(Protocol):
    """Stable test DTO boundary; production signatures are translated in one binding module."""

    def run_work_unit(
        self, request: WorkUnitRequest, services: ServiceBundle
    ) -> WorkUnitResult: ...

    def deliver(self, request: DeliveryRequest, services: ServiceBundle) -> DeliveryResult: ...

    def publish_outcome(self, request: NotificationRequest, services: ServiceBundle) -> None: ...

    def run_phase6_flow(
        self, request: Phase6FlowRequest, services: ServiceBundle
    ) -> Phase6FlowResult:
        """Run Production orchestration, including terminal notification/summary handling."""
        ...


def require_status(result: WorkUnitResult | DeliveryResult, expected: str) -> None:
    assert result.status.upper() == expected, (
        f"expected {expected}, got {result.status}: {result.reason}"
    )
