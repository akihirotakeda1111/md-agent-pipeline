from __future__ import annotations

import pytest

from .common import (
    assert_no_codex,
    coderabbit_completed,
    current_feedback,
    github_responses,
    request,
)
from .harness.adapters import require_status
from .harness.fake_classifier import ClassifierStep, classification
from .harness.fake_codex import CodexStep


def test_actionable_high_confidence_allowed_path_runs_repair(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification("ACTIONABLE", confidence=0.93)],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "REVIEW_FIX_PUSHED")
    assert len(services.classifier.invocations) == 1
    assert len(services.codex.invocations) == 1


def test_actionable_low_confidence_escalates_without_codex(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification("ACTIONABLE", confidence=0.79)],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert_no_codex(services)


def test_actionable_referenced_path_outside_allowed_scope_escalates_without_codex(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification("ACTIONABLE", paths=("docs/README.md",))],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert_no_codex(services)


def test_non_actionable_has_no_code_change_and_converges(
    phase7_driver, spec_path, git_repo, service_factory
):
    feedback = current_feedback(git_repo, "non-actionable-current.json")
    head = git_repo.head
    services = service_factory(
        github=github_responses(git_repo, [feedback], **coderabbit_completed(git_repo)),
        classifier=[classification("NON_ACTIONABLE", paths=())],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "READY_FOR_HUMAN")
    assert git_repo.head == head
    assert_no_codex(services)


@pytest.mark.parametrize(
    "classification_name", ["OUT_OF_SCOPE", "CONFLICTS_WITH_SPEC", "UNCERTAIN"]
)
def test_escalating_classifications_do_not_run_codex(
    classification_name, phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification(classification_name)],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert_no_codex(services)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"classification":"ACTIONABLE"}',
        '{"classification":"ALLOW","confidence":1.0,"reason":"x","referencedPaths":[]}',
        '{"classification":"ACTIONABLE","confidence":2.0,"reason":"x","referencedPaths":[]}',
    ],
)
def test_invalid_classifier_output_fails_closed(
    raw, phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[ClassifierStep(raw)],
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    require_status(result, "ESCALATED")
    assert_no_codex(services)
