from __future__ import annotations

import pytest

from .common import (
    PR_NUMBER,
    assert_no_codex,
    assert_no_git_write,
    current_feedback,
    github_responses,
    request,
    snapshot,
)
from .harness.fake_classifier import classification
from .harness.fake_codex import CodexStep


def test_event_is_only_wakeup_and_current_feedback_is_refetched(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo)
    services = service_factory(
        github=github_responses(git_repo, [feedback]),
        classifier=[classification("NON_ACTIONABLE", paths=())],
    )
    phase7_driver.run_review(
        request(spec_path, git_repo, object_id="wake-up-object-not-in-current-set"), services
    )
    assert len(services.github.calls("get_pull_request")) == 2
    assert len(services.github.calls("list_review_feedback")) == 1
    assert services.classifier.invocations[0]["payload"]["body"] == feedback["body"]


def test_non_coderabbit_actor_is_rejected_before_classifier(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(github=github_responses(git_repo, []))
    result = phase7_driver.run_review(
        request(spec_path, git_repo, actor="untrusted-user"), services
    )
    assert result.status.upper() in {"REJECTED", "SKIPPED", "ESCALATED"}
    assert_no_codex(services)
    assert services.classifier.invocations == []


def test_event_pr_number_mismatch_does_not_classify_repair_or_become_ready(
    phase7_driver, spec_path, git_repo, service_factory
):
    event_pr_number = PR_NUMBER
    api_pr_number = 99
    assert event_pr_number != api_pr_number
    review_request = request(spec_path, git_repo)
    assert review_request.event.pr_number == event_pr_number
    responses = github_responses(git_repo, [current_feedback(git_repo)])
    responses["get_pull_request"][0]["number"] = api_pr_number
    services = service_factory(
        github=responses,
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    before = snapshot(git_repo)
    result = phase7_driver.run_review(review_request, services)
    assert len(services.classifier.invocations) == 0
    assert_no_codex(services)
    assert_no_git_write(before, git_repo)
    assert result.status.upper() != "READY_FOR_HUMAN"


@pytest.mark.parametrize(
    "mutation",
    [
        {"work_unit_id": "other-work-unit"},
        {"head": {"sha": "outdated", "ref": "agent/phase7-integration"}},
    ],
)
def test_pr_work_unit_or_head_mismatch_fails_closed(
    mutation, phase7_driver, spec_path, git_repo, service_factory
):
    pull = github_responses(git_repo, [])
    pull["get_pull_request"][0].update(mutation)
    services = service_factory(github=pull)
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    assert result.status.upper() in {"SKIPPED", "REJECTED", "ESCALATED"}
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert_no_codex(services)
    assert services.classifier.invocations == []


def test_outdated_feedback_is_skipped_before_classifier(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo)
    feedback["head_sha"] = "obsolete-head"
    services = service_factory(github=github_responses(git_repo, [feedback]))
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    assert result.status.upper() in {"IN_REVIEW", "SKIPPED"}
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert_no_codex(services)
    assert services.classifier.invocations == []


def test_obvious_forbidden_path_is_rejected_before_classifier(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo)
    feedback["path"] = "specs/tasks/phase7-integration.md"
    services = service_factory(github=github_responses(git_repo, [feedback]))
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    assert result.status.upper() in {"SKIPPED", "ESCALATED"}
    assert_no_codex(services)
    assert services.classifier.invocations == []
