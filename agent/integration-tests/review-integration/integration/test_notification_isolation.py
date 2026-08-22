from __future__ import annotations

from agent.errors import AgentError

from .common import coderabbit_completed, github_responses, request
from .harness.observations import event_names


def test_review_notification_failure_keeps_primary_result(
    phase7_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        github=github_responses(git_repo, [], **coderabbit_completed(git_repo))
    )
    services.github.fail(
        "list_check_runs",
        AgentError.environment_failure("timeout", code="GITHUB_API_TIMEOUT"),
    )
    services.github.fail(
        "add_pr_comment",
        AgentError.environment_failure("comment failed", code="GITHUB_API_FAILURE"),
    )
    result = phase7_driver.run_review(request(spec_path, git_repo), services)
    events = result.events or services.observations.events
    names = event_names(events)
    assert result.status.upper() == "FAILED"
    assert result.reason == "GITHUB_API_TIMEOUT"
    assert names.count("FAILED") == 1
    assert "NOTIFICATION_FAILED" in names
    assert names.index("FAILED") < names.index("NOTIFICATION_FAILED")
    diagnostic = next(item for item in events if item.get("event") == "NOTIFICATION_FAILED")
    assert diagnostic["phase"] == "review"
    assert diagnostic["primary_outcome"] == "FAILED"
    assert diagnostic["primary_code"] == "GITHUB_API_TIMEOUT"
    assert diagnostic["notification_operation"] == "create_issue_comment"
    assert diagnostic["notification_error_code"] == "GITHUB_API_FAILURE"
    assert diagnostic["notification_error_code"] != diagnostic["primary_code"]
    assert services.github.calls("set_labels")
