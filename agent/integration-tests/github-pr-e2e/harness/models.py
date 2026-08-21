from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ProductionGap(RuntimeError):
    def __init__(
        self,
        gap_id: str,
        *,
        contract: str,
        observed: str,
        impact: str,
        required_change: str,
    ) -> None:
        self.gap_id = gap_id
        self.details = {
            "id": gap_id,
            "contract": contract,
            "observed": observed,
            "impact": impact,
            "required_production_change": required_change,
        }
        super().__init__(f"PRODUCTION_GAP: {gap_id}: {observed}")


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
    generated_content: str


@dataclass(frozen=True)
class WorkflowInfo:
    id: int
    name: str
    path: str
    state: str
    trigger: str
    dispatch_inputs: dict[str, Any]


@dataclass(frozen=True)
class RunEvidence:
    id: int
    attempt: int
    workflow_url: str
    sha: str
    branch: str
    event: str
    conclusion: str
    jobs: dict[str, str]


@dataclass(frozen=True)
class PullRequestEvidence:
    number: int
    url: str
    head: str
    base: str
    head_sha: str
    body: str
    labels: tuple[str, ...]
    author: str = ""


def report_skeleton(scenario: Scenario) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "repository": scenario.repo,
        "source_branch": scenario.source_branch,
        "task_spec": scenario.task_spec,
        "task_id": scenario.task_id,
        "target_branch": scenario.target_branch,
        "base_branch": scenario.base_branch,
        "generated_file": scenario.generated_file,
        "run1": {},
        "run2": {},
        "pr_count": None,
        "cleanup": {},
        "result": "RUNNING",
        "errors": [],
    }
