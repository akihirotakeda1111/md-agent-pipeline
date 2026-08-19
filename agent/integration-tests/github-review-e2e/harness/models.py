from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ClassifiedFailure(RuntimeError):
    category = "E2E_BUG"

    def __init__(self, message: str, *, evidence: dict[str, Any] | None = None) -> None:
        self.evidence = evidence or {}
        super().__init__(message)


class ProductionBug(ClassifiedFailure):
    category = "PRODUCTION_BUG"


class EnvironmentBlocker(ClassifiedFailure):
    category = "ENVIRONMENT_BLOCKER"


class ExternalServiceBlocker(ClassifiedFailure):
    category = "EXTERNAL_SERVICE_BLOCKER"


class E2EBug(ClassifiedFailure):
    category = "E2E_BUG"


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    repo: str
    base_branch: str
    source_branch: str
    task_spec: str
    task_id: str
    target_branch: str
    generated_file: str


@dataclass(frozen=True)
class WorkflowInfo:
    id: int
    name: str
    path: str
    state: str
    triggers: tuple[str, ...]


@dataclass(frozen=True)
class RunEvidence:
    id: int
    attempt: int
    workflow_url: str
    sha: str
    branch: str
    event: str
    actor: str
    status: str
    conclusion: str
    jobs: dict[str, str]
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class PullRequestEvidence:
    number: int
    url: str
    head: str
    base: str
    head_sha: str
    body: str
    labels: tuple[str, ...]
    state: str
    merged: bool
    merged_at: str | None
    auto_merge: Any


@dataclass(frozen=True)
class FeedbackEvidence:
    kind: str
    source_id: int
    actor: str
    body: str
    path: str | None
    commit_sha: str | None
    created_at: str
    updated_at: str
    html_url: str | None


def report_skeleton(scenario: Scenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "repository": scenario.repo,
        "source_branch": scenario.source_branch,
        "base_branch": scenario.base_branch,
        "task_spec": scenario.task_spec,
        "task_id": scenario.task_id,
        "target_branch": scenario.target_branch,
        "generated_file": scenario.generated_file,
        "preflight": {},
        "execute": {},
        "scenario_a": {
            "name": "review-convergence-ready",
            "feedback": [],
            "review_runs": [],
            "head_history": [],
        },
        "scenario_b": {"name": "non-coderabbit-actor-fail-closed"},
        "pr_count": None,
        "cleanup": {},
        "blocker_or_failure": None,
        "errors": [],
        "result": "RUNNING",
    }
