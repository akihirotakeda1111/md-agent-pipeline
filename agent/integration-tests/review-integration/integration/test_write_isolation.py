from __future__ import annotations

from .common import current_feedback, github_responses, request
from .harness.fake_classifier import classification
from .harness.fake_codex import CodexStep


def test_codex_has_no_github_or_classifier_authority(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [current_feedback(git_repo)]),
        classifier=[classification("ACTIONABLE")],
        codex=[CodexStep({"app/review.txt": "repaired\n"})],
    )
    phase7_driver.run_review(request(spec_path, git_repo), services)
    assert len(services.codex.invocations) == 1
    codex_keys = set(services.codex.invocations[0]["env_keys"])
    assert "CODEX_API_KEY" in codex_keys
    assert "GITHUB_TOKEN" not in codex_keys
    assert "GH_TOKEN" not in codex_keys
    assert "GITHUB_PAT" not in codex_keys
    assert "REVIEW_CLASSIFIER_API_KEY" not in codex_keys
    classifier_keys = set(services.classifier.invocations[0]["env_keys"])
    assert "REVIEW_CLASSIFIER_API_KEY" in classifier_keys
    assert "CODEX_API_KEY" not in classifier_keys
    assert "GITHUB_TOKEN" not in classifier_keys
