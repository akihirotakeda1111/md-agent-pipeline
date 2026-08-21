from __future__ import annotations

from dataclasses import replace

from .common import (
    assert_linear_push,
    assert_no_codex,
    assert_no_git_write,
    coderabbit_completed,
    current_feedback,
    github_responses,
    request,
    snapshot,
)
from .harness.adapters import require_status
from .harness.fake_classifier import classification
from .harness.fake_codex import CodexStep
from .harness.observations import event_names


def actionable_services(service_factory, git_repo, change):
    return service_factory(
        github=github_responses(
            git_repo, [current_feedback(git_repo)], **coderabbit_completed(git_repo)
        ),
        classifier=[classification("ACTIONABLE", confidence=0.93)],
        codex=[CodexStep(change)],
    )


def test_repair_runs_scope_all_validation_final_verification_then_push(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = actionable_services(service_factory, git_repo, {"app/review.txt": "repaired\n"})
    before = snapshot(git_repo)
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "REVIEW_FIX_PUSHED")
    assert result.review_attempts == 1
    assert_linear_push(before, git_repo)
    names = event_names(result.events or services.observations.events)
    assert "REVIEW_FIX_STARTED" in names
    assert "REVIEW_FIX_VALIDATION_PASSED" in names


def test_attempt_limit_escalates_before_codex(phase7_driver, spec_path, git_repo, service_factory):
    services = service_factory(
        github=github_responses(
            git_repo, [current_feedback(git_repo)], **coderabbit_completed(git_repo)
        ),
        classifier=[classification("ACTIONABLE")],
    )
    review_request = replace(request(spec_path, git_repo), review_attempts=1)
    result = phase7_driver.run_review(review_request, services)
    require_status(result, "ESCALATED")
    assert_no_codex(services)
    assert result.review_attempts == 1


def test_scope_violation_does_not_commit_or_push(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = actionable_services(service_factory, git_repo, {"docs/outside.txt": "forbidden\n"})
    before = snapshot(git_repo)
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert_no_git_write(before, git_repo)


def test_task_validation_failure_does_not_commit_or_push(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = actionable_services(
        service_factory,
        git_repo,
        {"app/review.txt": "repaired\n", "app/task-two.txt": "broken\n"},
    )
    before = snapshot(git_repo)
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    assert result.status.upper() in {"FAILED", "ESCALATED"}
    assert_no_git_write(before, git_repo)


def test_final_verification_failure_does_not_commit_or_push(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = actionable_services(service_factory, git_repo, {"app/review.txt": "not-repaired\n"})
    before = snapshot(git_repo)
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    assert result.status.upper() in {"FAILED", "ESCALATED"}
    assert_no_git_write(before, git_repo)
