from __future__ import annotations

from .harness.workflow_contract import (
    checkout_steps,
    contains_reference,
    effective_permissions,
    is_false,
    load_workflow,
    scalar_text,
)


def review_workflow(production_root):
    return load_workflow(production_root / ".github" / "workflows" / "agent-review.yml")


def workflow_triggers(workflow):
    return workflow.get("on", workflow.get(True, {}))


def test_review_is_a_separate_async_workflow(production_root):
    workflow = review_workflow(production_root)
    trigger_text = scalar_text(workflow_triggers(workflow)).lower()
    assert any(
        name in trigger_text
        for name in (
            "pull_request_review",
            "pull_request_review_comment",
            "issue_comment",
            "check_run",
            "status",
        )
    )
    body = scalar_text(workflow).lower()
    assert "sleep " not in body
    assert "while true" not in body
    execute_path = production_root / ".github" / "workflows" / "agent-execute.yml"
    assert execute_path.is_file()
    execute_body = execute_path.read_text(encoding="utf-8").lower()
    assert "sleep " not in execute_body
    assert "while true" not in execute_body


def test_same_pr_concurrency_contract(production_root):
    workflow = review_workflow(production_root)
    assert workflow.get("concurrency") in (None, {}), (
        "workflow-level concurrency deadlocks the review job on the same group"
    )
    prepare = workflow["jobs"]["prepare"]
    assert prepare.get("concurrency") in (None, {}), (
        "prepare must not take a concurrency lock"
    )
    review = workflow["jobs"]["review"]
    concurrency = review.get("concurrency")
    assert isinstance(concurrency, dict), "review job must define PR-scoped concurrency"
    group = str(concurrency.get("group", ""))
    assert "github.workflow" in group
    assert "needs.prepare.outputs.pull_number" in group
    assert is_false(concurrency.get("cancel-in-progress")), "same-PR review runs must serialize"


def test_permissions_are_explicit_and_minimal(production_root):
    workflow = review_workflow(production_root)
    assert isinstance(workflow.get("permissions"), dict)
    for name in workflow["jobs"]:
        permissions = effective_permissions(workflow, name)
        assert set(permissions) <= {"contents", "pull-requests", "issues", "checks"}
        assert all(value in {"read", "write", "none"} for value in permissions.values())
    combined = {
        key: value
        for name in workflow["jobs"]
        for key, value in effective_permissions(workflow, name).items()
    }
    assert combined.get("contents") == "write"
    assert combined.get("pull-requests") == "write"


def test_checkout_does_not_persist_credentials(production_root):
    workflow = review_workflow(production_root)
    steps = checkout_steps(workflow)
    assert steps
    for step in steps:
        assert is_false((step.get("with") or {}).get("persist-credentials"))


def test_classifier_and_codex_credentials_are_separated(production_root):
    workflow = review_workflow(production_root)
    assert not contains_reference(workflow.get("env", {}), "CODEX_API_KEY")
    assert not contains_reference(workflow.get("env", {}), "REVIEW_CLASSIFIER_API_KEY")
    for job in workflow["jobs"].values():
        assert not contains_reference(job.get("env", {}), "CODEX_API_KEY")
        assert not contains_reference(job.get("env", {}), "REVIEW_CLASSIFIER_API_KEY")
    prepare = workflow["jobs"]["prepare"]
    assert not contains_reference(prepare, "CODEX_API_KEY")
    assert not contains_reference(prepare, "REVIEW_CLASSIFIER_API_KEY")
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]
    orchestrator = [step for step in steps if "run-review.py" in scalar_text(step)]
    assert orchestrator, "review orchestrator step must exist"
    orchestrator_env = orchestrator[0].get("env", {})
    assert contains_reference(orchestrator_env, "CODEX_API_KEY")
    assert contains_reference(orchestrator_env, "REVIEW_CLASSIFIER_API_KEY")
    for step in steps:
        if step in orchestrator:
            continue
        assert not contains_reference(step.get("env", {}), "CODEX_API_KEY")
        assert not contains_reference(step.get("env", {}), "REVIEW_CLASSIFIER_API_KEY")


def test_codex_action_allows_only_configured_coderabbit_bot(production_root):
    workflow = review_workflow(production_root)
    review = workflow["jobs"]["review"]
    assert review["if"] == "${{ needs.prepare.outputs.should_review == 'true' }}"
    prepare = workflow["jobs"]["prepare"]
    assert "coderabbit_actor" in prepare["outputs"]
    assert "steps.gate.outputs.coderabbit_actor" in str(prepare["outputs"]["coderabbit_actor"])
    bootstrap = next(
        step
        for step in review["steps"]
        if str(step.get("uses", "")).startswith("openai/codex-action@")
    )
    inputs = bootstrap.get("with") or {}
    assert inputs.get("allow-bots") in (None, False, "false")
    allow_bots_users = str(inputs.get("allow-bot-users") or "")
    assert allow_bots_users == "${{ needs.prepare.outputs.coderabbit_actor }}"
    assert "*" not in allow_bots_users
    assert "coderabbitai[bot]" not in allow_bots_users
    assert inputs.get("prompt") in (None, "")
    assert inputs.get("prompt-file") in (None, "")
    assert "GITHUB_TOKEN" not in str(inputs)
    assert "REVIEW_CLASSIFIER_API_KEY" not in str(bootstrap)
