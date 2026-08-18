from __future__ import annotations

from .common import NEW_PR_GITHUB, delivery_request, github_fixture
from .harness.adapters import Phase6FlowRequest, WorkUnitRequest
from .harness.fake_codex import CodexStep
from .harness.observations import assert_in_order, event_names


def test_work_unit_emits_major_structured_events(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[CodexStep({"app/task-1.txt": "one\n"}), CodexStep({"app/task-2.txt": "two\n"})]
    )
    result = phase6_driver.run_work_unit(
        WorkUnitRequest(spec_path, git_repo.root, False, {"CODEX_API_KEY": "test"}), services
    )
    events = result.events or services.observations.events
    assert all(isinstance(item, dict) and ("event" in item or "type" in item) for item in events)
    names = event_names(events)
    assert_in_order(
        names,
        [
            "SPEC_DISCOVERED",
            "SPEC_VALIDATED",
            "TASK_STARTED",
            "CODEX_STARTED",
            "CODEX_COMPLETED",
            "SCOPE_CHECK_STARTED",
            "SCOPE_CHECK_PASSED",
            "VALIDATION_STARTED",
            "VALIDATION_PASSED",
            "TASK_COMPLETED",
            "FINAL_VALIDATION_STARTED",
            "FINAL_VALIDATION_PASSED",
            "WORKFLOW_COMPLETED",
        ],
    )


def test_repair_emits_validation_failed_then_repair_started(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[
            CodexStep(),
            CodexStep({"app/task-1.txt": "one\n"}),
            CodexStep({"app/task-2.txt": "two\n"}),
        ]
    )
    result = phase6_driver.run_work_unit(
        WorkUnitRequest(spec_path, git_repo.root, False, {"CODEX_API_KEY": "test"}), services
    )
    assert_in_order(
        event_names(result.events or services.observations.events),
        ["VALIDATION_FAILED", "REPAIR_STARTED"],
    )


def test_new_delivery_emits_delivery_validation_and_pr_created(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses=NEW_PR_GITHUB)
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    assert_in_order(
        event_names(result.events or services.observations.events),
        [
            "DELIVERY_VALIDATION_STARTED",
            "DELIVERY_VALIDATION_PASSED",
            "PR_CREATED",
            "WORKFLOW_COMPLETED",
        ],
    )


def test_full_production_flow_uses_contractual_partial_event_order(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[
            CodexStep({"app/task-1.txt": "one\n"}),
            CodexStep({"app/task-2.txt": "two\n"}),
        ],
        github_responses=NEW_PR_GITHUB,
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
        ),
        services,
    )
    names = event_names(result.events or services.observations.events)
    assert_in_order(
        names,
        [
            "FINAL_VALIDATION_PASSED",
            "DELIVERY_VALIDATION_STARTED",
            "DELIVERY_VALIDATION_PASSED",
            "PR_CREATED",
        ],
    )


def test_reuse_does_not_emit_new_delivery_validation(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(
        github_responses={"list_pull_requests": [[github_fixture("existing-same-work-unit.json")]]}
    )
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    names = event_names(result.events or services.observations.events)
    assert "DELIVERY_VALIDATION_STARTED" not in names
    assert "DELIVERY_VALIDATION_PASSED" not in names
    assert not services.github.calls("create_pull_request")


def test_full_production_reuse_flow_omits_new_delivery_events(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[
            CodexStep({"app/task-1.txt": "one\n"}),
            CodexStep({"app/task-2.txt": "two\n"}),
        ],
        github_responses={"list_pull_requests": [[github_fixture("existing-same-work-unit.json")]]},
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
        ),
        services,
    )
    names = event_names(result.events or services.observations.events)
    assert "FINAL_VALIDATION_PASSED" in names
    assert "DELIVERY_VALIDATION_STARTED" not in names
    assert "DELIVERY_VALIDATION_PASSED" not in names
    assert not services.github.calls("create_pull_request")


def test_escalated_and_failed_events_are_distinct(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    escalated_services = service_factory(github_responses={"list_pull_requests": [[]]})
    escalated = phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
        ),
        escalated_services,
    )
    assert "ESCALATED" in event_names(escalated.events or escalated_services.observations.events)

    failed_services = service_factory(
        codex_steps=[CodexStep(exit_code=75, final_message="could not resolve host")]
    )
    failed = phase6_driver.run_work_unit(
        WorkUnitRequest(spec_path, git_repo.root, False, {"CODEX_API_KEY": "test"}), failed_services
    )
    assert "FAILED" in event_names(failed.events or failed_services.observations.events)
