from __future__ import annotations

from .harness.workflow_contract import (
    checkout_steps,
    codex_steps,
    contains_reference,
    effective_permissions,
    is_false,
    is_zero,
    load_workflow,
)


def test_workflow_execute_and_deliver_permissions_are_explicit(production_root):
    workflow = load_workflow(production_root / ".github" / "workflows" / "agent-execute.yml")
    assert "execute" in workflow["jobs"]
    assert "deliver" in workflow["jobs"]

    execute = effective_permissions(workflow, "execute")
    assert execute.get("contents") == "read"
    assert all(value != "write" for value in execute.values())

    deliver = effective_permissions(workflow, "deliver")
    assert deliver.get("contents") == "write"
    assert deliver.get("pull-requests") == "write"
    assert deliver.get("issues") == "write"


def test_workflow_codex_secret_exists_only_in_execute_job(production_root):
    workflow = load_workflow(production_root / ".github" / "workflows" / "agent-execute.yml")
    assert not contains_reference(workflow.get("env", {}), "CODEX_API_KEY")
    jobs = workflow["jobs"]
    assert contains_reference(jobs["execute"], "CODEX_API_KEY")
    for name, job in jobs.items():
        if name != "execute":
            assert not contains_reference(job, "CODEX_API_KEY"), f"CODEX_API_KEY exposed to {name}"


def test_codex_steps_have_no_github_write_credential_or_authority(production_root):
    workflow = load_workflow(production_root / ".github" / "workflows" / "agent-execute.yml")
    execute_job = workflow["jobs"]["execute"]
    steps = codex_steps(execute_job)
    assert steps, "No Codex execution/bootstrap step found"
    assert all(value != "write" for value in effective_permissions(workflow, "execute").values())
    for step in steps:
        env = step.get("env") or {}
        # Production isolates Codex by setting GITHUB_TOKEN to empty, not by omitting the key.
        assert not env.get("GITHUB_TOKEN"), "Codex step has a usable GITHUB_TOKEN"
        assert not env.get("GH_TOKEN"), "Codex step has a usable GH_TOKEN"
        assert not env.get("AGENT_PR_PAT"), "Codex step has a usable AGENT_PR_PAT"
        with_inputs = step.get("with") or {}
        assert not with_inputs.get("github-token"), "Codex step has a usable github-token"


def test_checkout_does_not_persist_credentials_and_fetches_full_history(production_root):
    workflow = load_workflow(production_root / ".github" / "workflows" / "agent-execute.yml")
    execute_checkouts = checkout_steps(workflow["jobs"]["execute"])
    assert execute_checkouts, "No actions/checkout step in execute"
    for step in execute_checkouts:
        inputs = step.get("with") or {}
        assert is_false(inputs.get("persist-credentials")), "execute persists checkout credentials"
        assert is_zero(inputs.get("fetch-depth")), "execute does not fetch full history"

    deliver_checkouts = checkout_steps(workflow["jobs"]["deliver"])
    assert deliver_checkouts, "No actions/checkout step in deliver"
    for step in deliver_checkouts:
        inputs = step.get("with") or {}
        # persist-credentials would let Final Verification reuse git write auth.
        assert is_false(inputs.get("persist-credentials")), "deliver persists checkout credentials"
        assert is_zero(inputs.get("fetch-depth")), "deliver does not fetch full history"


def test_deliver_job_has_no_codex_credential(production_root):
    workflow = load_workflow(production_root / ".github" / "workflows" / "agent-execute.yml")
    assert not contains_reference(workflow["jobs"]["deliver"], "CODEX_API_KEY")
    assert contains_reference(workflow["jobs"]["deliver"], "AGENT_PR_PAT")
    assert not contains_reference(workflow["jobs"]["execute"], "AGENT_PR_PAT")
    assert not contains_reference(workflow["jobs"]["parse-spec"], "AGENT_PR_PAT")
