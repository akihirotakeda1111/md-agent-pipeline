from __future__ import annotations

import json

from .common import delivery_request, github_fixture, issue_has_label
from .harness.adapters import Phase6FlowRequest
from .harness.fake_codex import CodexStep


def test_29_temporary_environment_failure_is_failed(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[CodexStep(exit_code=75, final_message="could not resolve host")],
        github_responses={
            "create_issue": [{"number": 1, "html_url": "https://example.invalid/issues/1"}]
        },
    )
    result = phase6_driver.run_phase6_flow(
        Phase6FlowRequest(
            spec_path=spec_path,
            repo_root=git_repo.root,
            execute_environment={"CODEX_API_KEY": "test"},
            deliver_environment={
                "GITHUB_TOKEN": "github-test",
                "GITHUB_REPOSITORY": "example/phase6",
            },
        ),
        services,
    )
    assert result.status.upper() == "FAILED"
    assert issue_has_label(services, "agent:failed")


def test_30_unsafe_report_inconsistency_is_escalated(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    result = phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"branch": "unsafe"}),
        ),
        service_factory(),
    )
    assert result.status.upper() == "ESCALATED"


def test_31_pr_present_escalation_comments_and_labels(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    existing = github_fixture("existing-wrong-marker.json")
    services = service_factory(github_responses={"list_pull_requests": [[existing], [existing]]})
    phase6_driver.deliver(
        delivery_request(
            spec_path, git_repo, artifact_factory(spec_path), mention="@configured-reviewer"
        ),
        services,
    )
    assert services.github.calls("add_pr_comment")
    assert any(
        "agent:escalated" in json.dumps(call) for call in services.github.calls("set_labels")
    )


def test_32_no_pr_escalation_uses_issue_or_documented_notification_route(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
        ),
        services,
    )
    assert services.github.calls("create_issue")


def test_33_notification_payload_contains_required_fields(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
        ),
        services,
    )
    payload = json.dumps(services.github.calls("create_issue")[0]).lower()
    for field in [
        "task id",
        "current task",
        "reason",
        "last validation",
        "repair attempts",
        "required human action",
    ]:
        assert field in payload


def test_34_only_configured_mention_is_used(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
            mention="@configured-reviewer",
        ),
        services,
    )
    payload = json.dumps(services.github.calls("create_issue")[0])
    assert "@configured-reviewer" in payload


def test_35_no_mention_does_not_invent_username(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
        ),
        services,
    )
    payload = json.dumps(services.github.calls("create_issue")[0])
    assert "@" not in payload


def test_production_failed_flow_publishes_notification_label_and_summary(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[CodexStep(exit_code=75, final_message="could not resolve host")],
        github_responses={
            "create_issue": [{"number": 1, "html_url": "https://example.invalid/issues/1"}]
        },
    )
    result = phase6_driver.run_phase6_flow(
        Phase6FlowRequest(
            spec_path=spec_path,
            repo_root=git_repo.root,
            execute_environment={"CODEX_API_KEY": "codex-test"},
            deliver_environment={
                "GITHUB_TOKEN": "github-test",
                "GITHUB_REPOSITORY": "example/phase6",
            },
            notification_mention="@configured-reviewer",
        ),
        services,
    )
    assert result.status.upper() == "FAILED"
    assert services.github.calls("create_issue")
    assert issue_has_label(services, "agent:failed")
    payload = json.dumps(services.github.calls("create_issue")[0])
    assert "@configured-reviewer" in payload
    payload_lower = payload.lower()
    for field in [
        "task id",
        "current task",
        "reason",
        "last validation",
        "repair attempts",
        "required human action",
    ]:
        assert field in payload_lower
    assert result.reason and result.reason in result.summary
    assert "Failure Reason" in result.summary


def test_production_unsafe_reconciliation_flow_escalates_comments_labels_and_summarizes(
    phase6_driver, spec_path, git_repo, service_factory
):
    existing = github_fixture("existing-wrong-marker.json")
    services = service_factory(
        codex_steps=[
            CodexStep({"app/task-1.txt": "one\n"}),
            CodexStep({"app/task-2.txt": "two\n"}),
        ],
        github_responses={"list_pull_requests": [[existing], [existing]]},
    )
    result = phase6_driver.run_phase6_flow(
        Phase6FlowRequest(
            spec_path=spec_path,
            repo_root=git_repo.root,
            execute_environment={"CODEX_API_KEY": "codex-test"},
            deliver_environment={
                "GITHUB_TOKEN": "github-test",
                "GITHUB_REPOSITORY": "example/phase6",
            },
            notification_mention="@configured-reviewer",
        ),
        services,
    )
    assert result.status.upper() == "ESCALATED"
    assert services.github.calls("add_pr_comment")
    assert any(
        "agent:escalated" in json.dumps(call) for call in services.github.calls("set_labels")
    )
    payload = json.dumps(services.github.calls("add_pr_comment")[0]).lower()
    for field in [
        "task id",
        "current task",
        "reason",
        "last validation",
        "repair attempts",
        "required human action",
    ]:
        assert field in payload
    assert "@configured-reviewer" in payload
    assert result.reason and result.reason in result.summary
    assert "Escalation Reason" in result.summary


def test_notification_failure_keeps_escalated_primary(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    from agent.errors import AgentError

    from .harness.observations import event_names

    existing = github_fixture("existing-wrong-marker.json")
    services = service_factory(github_responses={"list_pull_requests": [[existing], [existing]]})
    services.github.fail(
        "add_pr_comment",
        AgentError.environment_failure("comment failed", code="GITHUB_API_FAILURE"),
    )
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)),
        services,
    )
    names = event_names(result.events or services.observations.events)
    assert result.status.upper() == "ESCALATED"
    assert result.reason == "WORK_UNIT_PR_MISMATCH"
    assert result.summary
    assert "WORK_UNIT_PR_MISMATCH" in result.summary
    assert names.count("ESCALATED") == 1
    assert names.count("WORKFLOW_COMPLETED") == 1
    assert names.index("ESCALATED") < names.index("WORKFLOW_COMPLETED")
    assert names.index("WORKFLOW_COMPLETED") < names.index("NOTIFICATION_FAILED")
    assert services.github.calls("set_labels")
    assert not services.github.calls("create_issue")
    diagnostic = next(
        item
        for item in (result.events or services.observations.events)
        if item.get("event") == "NOTIFICATION_FAILED"
    )
    assert diagnostic["primary_code"] == "WORK_UNIT_PR_MISMATCH"
    assert diagnostic["notification_operation"] == "create_issue_comment"
    assert diagnostic["notification_error_code"] == "GITHUB_API_FAILURE"


def test_pr_lookup_failure_does_not_create_issue(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    from agent.errors import AgentError

    from .harness.observations import event_names

    services = service_factory()
    services.github.fail(
        "list_pull_requests",
        AgentError.environment_failure("timeout", code="GITHUB_API_TIMEOUT"),
    )
    result = phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
        ),
        services,
    )
    names = event_names(result.events or services.observations.events)
    assert result.status.upper() == "ESCALATED"
    assert result.reason == "SPEC_IDENTITY_MISMATCH"
    assert not services.github.calls("create_issue")
    assert "NOTIFICATION_FAILED" in names
    diagnostic = next(
        item
        for item in (result.events or services.observations.events)
        if item.get("event") == "NOTIFICATION_FAILED"
    )
    assert diagnostic["notification_operation"] == "list_open_pulls"
    assert diagnostic["primary_code"] == "SPEC_IDENTITY_MISMATCH"
