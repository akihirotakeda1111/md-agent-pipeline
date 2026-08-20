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


def _matching_jobs(run: RunEvidence, expected: str) -> list[tuple[str, str]]:
    return [
        (name, conclusion)
        for name, conclusion in run.jobs.items()
        if _job_matches(name, expected)
    ]


def assert_execute_run(
    run: RunEvidence,
    workflow: WorkflowInfo,
    scenario: Scenario,
    source_sha: str,
    trigger: str,
) -> None:
    assert run.attempt == 1, f"expected execute attempt 1, got {run.attempt}"
    assert run.sha == source_sha
    assert run.branch == scenario.source_branch
    assert run.event == trigger
    assert run.conclusion == "success", f"execute workflow conclusion: {run.conclusion}"
    for expected in ("parse spec", "execute", "deliver"):
        matches = _matching_jobs(run, expected)
        assert len(matches) == 1, f"expected exactly one {expected} job, got {matches}"
        assert matches[0][1] == "success", f"{expected} job conclusion: {matches[0][1]}"
    assert workflow.path.endswith("agent-execute.yml")


def assert_pr(pr: PullRequestEvidence, scenario: Scenario) -> None:
    assert pr.head == scenario.target_branch
    assert pr.base == scenario.base_branch
    assert pr.state == "open"
    assert not pr.merged and pr.merged_at is None
    assert pr.auto_merge is None, "automatic merge is enabled for the E2E PR"
    lowered = pr.body.lower()
    for section in REQUIRED_PR_SECTIONS:
        assert section.lower() in lowered, f"PR body missing section: {section}"
    assert re.search(r"agent[-_ ]?work[-_ ]?unit", pr.body, re.IGNORECASE)
    assert scenario.task_id in pr.body
    assert "agent:review" in pr.labels


def assert_pr_scope(files: list[str], scenario: Scenario) -> None:
    assert files == [scenario.generated_file], f"unexpected E2E PR files: {files}"
    assert not any(
        path.startswith((".agent/state/", ".github/", "agent/", "specs/")) for path in files
    )


def assert_review_run(
    run: RunEvidence, *, configured_actor: str, supported_events: tuple[str, ...]
) -> None:
    assert run.event in supported_events
    if run.event in {"pull_request_review", "pull_request_review_comment", "issue_comment"}:
        assert run.actor == configured_actor, (
            f"review workflow actor {run.actor!r} does not match configured actor {configured_actor!r}"
        )
    assert run.conclusion == "success", f"review workflow conclusion: {run.conclusion}"
    prepare = _matching_jobs(run, "prepare review")
    review = _matching_jobs(run, "review and repair")
    assert len(prepare) == 1 and prepare[0][1] == "success", f"prepare job: {prepare}"
    assert len(review) == 1 and review[0][1] == "success", f"review job: {review}"


def assert_non_coderabbit_short_circuit(run: RunEvidence, configured_actor: str) -> None:
    assert run.actor != configured_actor
    assert run.conclusion == "success", f"negative actor workflow conclusion: {run.conclusion}"
    prepare = _matching_jobs(run, "prepare review")
    review = _matching_jobs(run, "review and repair")
    assert len(prepare) == 1 and prepare[0][1] == "success", f"prepare job: {prepare}"
    assert not review or all(conclusion == "skipped" for _, conclusion in review), (
        f"review job must not run for non-CodeRabbit actor: {review}"
    )
    forbidden_events = {
        "REVIEW_COLLECTED",
        "REVIEW_CLASSIFIED",
        "REVIEW_POLICY_APPLIED",
        "REVIEW_FIX_STARTED",
        "REVIEW_FIX_VALIDATION_PASSED",
    }
    assert not forbidden_events.intersection(run.events), (
        f"non-CodeRabbit wake-up crossed the semantic/review boundary: {run.events}"
    )


def terminal_state(pr: PullRequestEvidence) -> str | None:
    states = {
        "agent:ready": "READY_FOR_HUMAN",
        "agent:escalated": "ESCALATED",
        "agent:failed": "FAILED",
    }
    found = [value for label, value in states.items() if label in pr.labels]
    assert len(found) <= 1, f"multiple terminal labels on PR: {pr.labels}"
    return found[0] if found else None


def assert_tracking_current_head(
    comments: list[dict[str, object]], scenario: Scenario, final_head_sha: str
) -> None:
    assert len(comments) == 1, f"review tracking comment count must be 1, got {len(comments)}"
    body = str(comments[0].get("body") or "")
    assert scenario.task_id in body, "review tracking is not bound to the current work-unit"
    assert final_head_sha in body, "review tracking is not bound to the final current HEAD"


def assert_linear_head_change(compare: dict[str, object], scenario: Scenario) -> None:
    assert compare.get("status") == "ahead", f"repair push is not linear: {compare.get('status')}"
    assert int(compare.get("behind_by", 0)) == 0
    assert int(compare.get("ahead_by", 0)) >= 1
    raw_files = compare.get("files", [])
    files = sorted(
        str(file["filename"])
        for file in raw_files  # type: ignore[union-attr]
        if isinstance(file, dict) and "filename" in file
    )
    if files:
        assert files == [scenario.generated_file], f"review repair changed unexpected files: {files}"
