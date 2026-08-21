from __future__ import annotations

import json
import os

from .common import NEW_PR_GITHUB, delivery_request
from .harness.adapters import WorkUnitRequest
from .harness.fake_codex import CodexStep
from .harness.workflow_contract import contains_reference, effective_permissions, load_workflow


def test_36_40_execute_validation_git_and_deliver_credentials_are_isolated(
    phase6_driver, spec_path, git_repo, service_factory, production_root
):
    workflow = load_workflow(production_root / ".github" / "workflows" / "agent-execute.yml")
    execute = effective_permissions(workflow, "execute")
    deliver = effective_permissions(workflow, "deliver")
    assert execute.get("contents") == "read"
    assert all(value != "write" for value in execute.values())
    assert contains_reference(workflow["jobs"]["execute"], "CODEX_API_KEY")
    assert deliver.get("contents") == "write"
    assert deliver.get("pull-requests") == "write"
    assert deliver.get("issues") == "write"
    assert not contains_reference(workflow["jobs"]["deliver"], "CODEX_API_KEY")
    assert contains_reference(workflow["jobs"]["deliver"], "AGENT_PR_PAT")
    assert not contains_reference(workflow["jobs"]["execute"], "AGENT_PR_PAT")

    execute_services = service_factory(
        codex_steps=[CodexStep({"app/task-1.txt": "one\n"}), CodexStep({"app/task-2.txt": "two\n"})]
    )
    phase6_driver.run_work_unit(
        WorkUnitRequest(
            spec_path,
            git_repo.root,
            False,
            {"CODEX_API_KEY": "codex-test"},
        ),
        execute_services,
    )
    assert not execute_services.github.calls("create_pull_request")
    assert not execute_services.github.calls("create_issue")


def test_adapter_restores_process_env_between_execute_and_deliver(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    before = dict(os.environ)
    execute_services = service_factory(
        codex_steps=[CodexStep({"app/task-1.txt": "one\n"}), CodexStep({"app/task-2.txt": "two\n"})]
    )
    phase6_driver.run_work_unit(
        WorkUnitRequest(spec_path, git_repo.root, False, {"CODEX_API_KEY": "codex-must-not-leak"}),
        execute_services,
    )
    assert os.environ.get("CODEX_API_KEY") == before.get("CODEX_API_KEY")
    assert os.environ.get("GITHUB_TOKEN") == before.get("GITHUB_TOKEN")
    assert os.environ.get("AGENT_PR_PAT") == before.get("AGENT_PR_PAT")

    deliver_services = service_factory(github_responses=NEW_PR_GITHUB)
    phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)),
        deliver_services,
    )
    assert os.environ.get("CODEX_API_KEY") == before.get("CODEX_API_KEY")
    assert os.environ.get("GITHUB_TOKEN") == before.get("GITHUB_TOKEN")
    assert os.environ.get("AGENT_PR_PAT") == before.get("AGENT_PR_PAT")


def test_phase6_deliver_applies_review_waiting_label(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses=NEW_PR_GITHUB)
    phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    payload = json.dumps([item.payload for item in services.observations.github])
    assert "agent:review" in payload
    assert "agent:ready" not in payload
    assert "agent:running" not in payload
