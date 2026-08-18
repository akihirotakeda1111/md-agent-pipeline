from __future__ import annotations

import json

from .common import NEW_PR_GITHUB, delivery_request
from .harness.adapters import Phase6FlowRequest
from .harness.fake_codex import CodexStep

REQUIRED_FIELDS = [
    "Task Spec",
    "Task ID",
    "State",
    "Current Task",
    "Completed Tasks",
    "Changed Files",
    "Validation Results",
    "Repair Attempts",
    "PR URL",
    "Failure Reason",
    "Escalation Reason",
]


def test_job_summary_contains_required_fields_on_success(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses=NEW_PR_GITHUB)
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    for field in REQUIRED_FIELDS:
        assert field in result.summary


def test_job_summary_contains_failure_and_escalation_reason(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    result = phase6_driver.deliver(
        delivery_request(
            spec_path,
            git_repo,
            artifact_factory(spec_path, report_overrides={"spec_id": "wrong"}),
        ),
        services,
    )
    assert "Escalation Reason" in result.summary
    assert result.reason in result.summary


def test_repair_attempts_are_preserved_in_report_pr_and_summary(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[
            CodexStep(),
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
    assert result.status.upper() == "READY"
    report = json.loads(
        (git_repo.root.parent / "agent-report" / "report.json").read_text(encoding="utf-8")
    )
    assert report["repair_attempts"] == 1
    assert report["state"]["repairAttempts"] == 1
    body = services.github.calls("create_pull_request")[0]["body"]
    section = body.split("## Repair Attempts", 1)[1]
    assert section.strip().splitlines()[0].strip() == "1"
    assert "Repair Attempts: 1" in result.summary


def test_repair_attempts_accumulate_across_tasks_in_report_pr_and_summary(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[
            CodexStep(),
            CodexStep({"app/task-1.txt": "one\n"}),
            CodexStep(),
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
    assert result.status.upper() == "READY"
    assert len(services.codex.invocations) == 4
    report = json.loads(
        (git_repo.root.parent / "agent-report" / "report.json").read_text(encoding="utf-8")
    )
    assert report["repair_attempts"] == 2
    assert report["state"]["repairAttempts"] == 2
    body = services.github.calls("create_pull_request")[0]["body"]
    section = body.split("## Repair Attempts", 1)[1]
    assert section.strip().splitlines()[0].strip() == "2"
    assert "Repair Attempts: 2" in result.summary
