from __future__ import annotations

from .common import (
    current_feedback,
    github_responses,
    processed_record,
    request,
    coderabbit_completed,
)
from .harness.adapters import require_status
from .harness.fake_classifier import classification
from .harness.fake_codex import CodexStep


def test_duplicate_event_does_not_repeat_repair(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo)
    services = service_factory(
        github=github_responses(git_repo, [feedback], **coderabbit_completed(git_repo)),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    first = phase7_driver.run_review(
        request(spec_path, git_repo, auto_repair_enabled=True), services
    )
    require_status(first, "REVIEW_FIX_PUSHED")
    services.github.current("get_pull_request")["head"]["sha"] = git_repo.head
    services.github.current("list_review_feedback")[0]["head_sha"] = git_repo.head
    second = phase7_driver.run_review(
        request(spec_path, git_repo, head_sha=git_repo.head, auto_repair_enabled=True), services
    )
    assert second.status.upper() == "IN_REVIEW"
    assert len(services.codex.invocations) == 1
    assert len(services.github.calls("list_review_feedback")) == 2


def test_duplicate_status_alone_does_not_establish_readiness(
    phase7_driver, spec_path, git_repo, service_factory
):
    duplicate = current_feedback(git_repo)
    pending = current_feedback(git_repo, "non-actionable-current.json")
    pending["body"] = "Ambiguous request requiring human review."
    responses = github_responses(
        git_repo,
        [duplicate, pending],
        load_processed_reviews=[[processed_record(duplicate)]],
        **coderabbit_completed(git_repo),
    )
    services = service_factory(
        github=responses,
        classifier=[classification("UNCERTAIN", paths=())],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert len(services.classifier.invocations) == 1


def test_edited_feedback_is_reevaluated_as_new_revision(
    phase7_driver, spec_path, git_repo, service_factory
):
    original = current_feedback(git_repo)
    edited = current_feedback(git_repo, "edited-current.json")
    responses = github_responses(
        git_repo,
        [edited],
        load_processed_reviews=[[processed_record(original)]],
        **coderabbit_completed(git_repo),
    )
    services = service_factory(
        github=responses,
        classifier=[classification("NON_ACTIONABLE", paths=())],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "READY_FOR_HUMAN")
    assert len(services.classifier.invocations) == 1
    assert services.classifier.invocations[0]["payload"]["body"] == edited["body"]


def test_unprocessed_current_feedback_prevents_readiness(
    phase7_driver, spec_path, git_repo, service_factory
):
    processed = current_feedback(git_repo, "non-actionable-current.json")
    pending = current_feedback(git_repo)
    responses = github_responses(
        git_repo,
        [processed, pending],
        load_processed_reviews=[[processed_record(processed)]],
        **coderabbit_completed(git_repo),
    )
    services = service_factory(
        github=responses,
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(
        request(spec_path, git_repo, auto_repair_enabled=True), services
    )
    assert result.status.upper() == "REVIEW_FIX_PUSHED"
    assert result.status.upper() != "READY_FOR_HUMAN"


def test_all_current_feedback_processed_allows_readiness(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo, "non-actionable-current.json")
    responses = github_responses(
        git_repo,
        [feedback],
        load_processed_reviews=[[processed_record(feedback)]],
        **coderabbit_completed(git_repo),
    )
    services = service_factory(github=responses)
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "READY_FOR_HUMAN")
    assert services.classifier.invocations == []
    assert services.codex.invocations == []
    assert len(services.github.calls("list_review_feedback")) == 1
