from __future__ import annotations

import re

from .models import PullRequestEvidence, RunEvidence, Scenario, WorkflowInfo

REQUIRED_PR_SECTIONS = (
    "Task Spec",
    "Objective",
    "Completed Tasks",
    "Changed Files",
    "Validation Results",
    "Final Verification",
    "Repair Attempts",
)


def _job_matches(name: str, expected: str) -> bool:
    lowered = name.lower().strip()
    token = expected.lower()
    return lowered == token or bool(re.match(rf"^{re.escape(token)}(?:\s|/|\()", lowered))


def assert_successful_run(
    run: RunEvidence,
    workflow: WorkflowInfo,
    scenario: Scenario,
    source_sha: str,
    expected_attempt: int,
    *,
    expected_event: str | None = None,
) -> None:
    assert run.attempt == expected_attempt, (
        f"expected run attempt {expected_attempt}, got {run.attempt}"
    )
    assert run.sha == source_sha
    assert run.branch == scenario.source_branch
    assert run.event == (expected_event or workflow.trigger)
    assert run.conclusion == "success", f"workflow conclusion: {run.conclusion}"
    for expected in ("execute", "deliver"):
        matches = [
            (name, conclusion)
            for name, conclusion in run.jobs.items()
            if _job_matches(name, expected)
        ]
        assert len(matches) == 1, f"expected exactly one {expected} job, got {matches}"
        assert matches[0][1] == "success", f"{expected} job conclusion: {matches[0][1]}"


def assert_source_tip_matches_run(run: RunEvidence, source_sha: str, branch_sha: str) -> None:
    assert run.sha == source_sha
    assert branch_sha == source_sha, (
        f"source branch tip {branch_sha} does not match run SHA {source_sha}"
    )


def assert_restart_run(
    run1: RunEvidence, run2: RunEvidence, *, expected_event: str, source_sha: str
) -> None:
    assert run2.id != run1.id, "Run 2 must be a new workflow run, not a same-run rerun"
    assert run2.attempt == 1, f"expected Run 2 attempt 1, got {run2.attempt}"
    assert run2.sha == run1.sha == source_sha
    assert run2.branch == run1.branch
    assert run2.event == expected_event


def assert_run_identity_count(matches: list[dict[str, object]]) -> None:
    assert len(matches) == 1, (
        f"workflow run identity must resolve to exactly one run, got {len(matches)}"
    )


def assert_pr(pr: PullRequestEvidence, scenario: Scenario) -> None:
    assert pr.head == scenario.target_branch
    assert pr.base == scenario.base_branch
    lowered = pr.body.lower()
    for section in REQUIRED_PR_SECTIONS:
        assert section.lower() in lowered, f"PR body missing section: {section}"
    assert re.search(r"agent[-_ ]?work[-_ ]?unit", pr.body, re.IGNORECASE), (
        "PR body missing work-unit marker"
    )
    assert scenario.task_id in pr.body, "PR body marker is not bound to Task ID"
    assert "agent:ready" in pr.labels


def assert_commit_files(files: list[str], scenario: Scenario) -> None:
    assert files == [scenario.generated_file], f"unexpected files in delivery commit: {files}"
    forbidden_prefixes = (".agent/state/", ".github/", "agent/", "specs/")
    assert not any(path.startswith(forbidden_prefixes) for path in files)


def assert_pr_files(files: list[str], scenario: Scenario) -> None:
    assert files == [scenario.generated_file], f"unexpected files in E2E PR: {files}"
    forbidden = [
        path for path in files if path.startswith((".agent/state/", ".github/", "agent/", "specs/"))
    ]
    assert not forbidden, f"forbidden paths in E2E PR: {forbidden}"


def assert_reuse(
    run1_pr: PullRequestEvidence, run2_pr: PullRequestEvidence, branch_sha: str
) -> None:
    assert run2_pr.number == run1_pr.number
    assert run2_pr.url == run1_pr.url
    assert run2_pr.head == run1_pr.head
    assert run2_pr.base == run1_pr.base
    assert run2_pr.head_sha == run1_pr.head_sha
    assert branch_sha == run1_pr.head_sha, "reuse created or moved the feature branch"


def assert_no_new_delivery_events(events: list[str]) -> None:
    forbidden = {"DELIVERY_VALIDATION_STARTED", "DELIVERY_VALIDATION_PASSED", "PR_CREATED"}
    found = sorted(forbidden.intersection(events))
    assert not found, f"reuse attempt emitted new-delivery event(s): {found}"
