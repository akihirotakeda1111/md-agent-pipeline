from __future__ import annotations

from .common import (
    assert_no_codex,
    assert_no_git_write,
    coderabbit_completed,
    coderabbit_skipped,
    current_feedback,
    github_responses,
    request,
    snapshot,
)
from .harness.adapters import require_status
from .harness.fake_classifier import classification
from .harness.fake_codex import CodexStep


def test_completed_terminal_with_zero_feedback_is_ready(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [], **coderabbit_completed(git_repo)),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "READY_FOR_HUMAN")
    assert services.classifier.invocations == []
    assert_no_codex(services)


def test_completed_non_actionable_is_ready(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo, "non-actionable-current.json")
    services = service_factory(
        github=github_responses(git_repo, [feedback], **coderabbit_completed(git_repo)),
        classifier=[classification("NON_ACTIONABLE", paths=())],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "READY_FOR_HUMAN")
    assert_no_codex(services)


def test_completed_actionable_repairs_and_is_not_ready(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(
            git_repo, [current_feedback(git_repo)], **coderabbit_completed(git_repo)
        ),
        classifier=[classification("ACTIONABLE", confidence=0.93)],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(
        request(spec_path, git_repo, auto_repair_enabled=True), services
    )
    require_status(result, "REVIEW_FIX_PUSHED")
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert len(services.classifier.invocations) == 1
    assert len(services.codex.invocations) == 1


def test_skipped_terminal_escalates_without_classifier_or_git_write(
    phase7_driver, spec_path, git_repo, service_factory
):
    before = snapshot(git_repo)
    services = service_factory(
        github=github_responses(
            git_repo, [current_feedback(git_repo)], **coderabbit_skipped(git_repo)
        ),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert services.classifier.invocations == []
    assert_no_codex(services)
    assert_no_git_write(before, git_repo)


def test_no_terminal_and_zero_feedback_stays_in_review(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, []),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "IN_REVIEW")
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert services.classifier.invocations == []
    assert_no_codex(services)


def test_old_head_completed_terminal_is_ignored(
    phase7_driver, spec_path, git_repo, service_factory
):
    stale = coderabbit_completed(git_repo)
    stale["list_check_runs"][0][0]["head_sha"] = "0" * 40
    services = service_factory(
        github=github_responses(git_repo, [], **stale),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "IN_REVIEW")
    assert result.status.upper() != "READY_FOR_HUMAN"
    assert services.classifier.invocations == []
    assert_no_codex(services)
