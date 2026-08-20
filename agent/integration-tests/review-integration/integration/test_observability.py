from __future__ import annotations

from .common import coderabbit_completed, current_feedback, github_responses, request
from .harness.fake_classifier import classification
from .harness.fake_codex import CodexStep
from .harness.observations import assert_in_order, event_names


def test_review_repair_emits_contractual_partial_order(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    events = result.events or services.observations.events
    assert all(isinstance(item, dict) and ("event" in item or "type" in item) for item in events)
    names = event_names(events)
    assert_in_order(
        names,
        [
            "REVIEW_RECEIVED",
            "REVIEW_COLLECTED",
            "REVIEW_CLASSIFIED",
            "REVIEW_POLICY_APPLIED",
            "REVIEW_FIX_STARTED",
            "REVIEW_FIX_VALIDATION_PASSED",
        ],
    )
    assert "READY_FOR_HUMAN" not in names


def test_escalation_emits_review_escalated(phase7_driver, spec_path, git_repo, service_factory):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification("UNCERTAIN")],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    assert_in_order(
        event_names(result.events or services.observations.events),
        ["REVIEW_CLASSIFIED", "REVIEW_POLICY_APPLIED", "REVIEW_ESCALATED"],
    )


def test_ready_event_is_terminal_after_collection_and_policy(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo, "non-actionable-current.json")
    services = service_factory(
        github=github_responses(git_repo, [feedback], **coderabbit_completed(git_repo)),
        classifier=[classification("NON_ACTIONABLE", paths=())],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    names = event_names(result.events or services.observations.events)
    assert_in_order(
        names,
        ["REVIEW_COLLECTED", "REVIEW_CLASSIFIED", "REVIEW_POLICY_APPLIED", "READY_FOR_HUMAN"],
    )
    assert names[-1] == "READY_FOR_HUMAN"
