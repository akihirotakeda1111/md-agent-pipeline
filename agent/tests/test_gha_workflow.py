from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-execute.yml"


def _load() -> tuple[dict[object, object], dict[object, object]]:
    payload = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    triggers = payload.get("on", payload.get(True))
    assert isinstance(triggers, dict)
    return payload, triggers


def test_workflow_yaml_parses_and_uses_path_trigger() -> None:
    payload, triggers = _load()
    assert payload["name"] == "Agent Execute"
    assert "push" in triggers
    assert triggers["push"]["paths"] == ["specs/tasks/**/*.md"]
    assert "branches" not in triggers["push"]
    assert "workflow_dispatch" in triggers
    assert triggers["workflow_dispatch"]["inputs"]["spec_path"]["required"] is True
    assert "pull_request" not in triggers
    assert "pull_request_target" not in triggers


def test_parse_job_exposes_required_outputs() -> None:
    payload, _ = _load()
    jobs = payload["jobs"]
    parse_job = jobs["parse-spec"]
    assert parse_job["outputs"]["task_id"] == "${{ steps.parse.outputs.task_id }}"
    assert parse_job["outputs"]["spec_path"] == "${{ steps.parse.outputs.spec_path }}"
    assert parse_job["outputs"]["base_branch"] == "${{ steps.parse.outputs.base_branch }}"
    assert parse_job["outputs"]["target_branch"] == "${{ steps.parse.outputs.target_branch }}"
    assert parse_job["outputs"]["valid"] == "${{ steps.parse.outputs.valid }}"
    assert parse_job["outputs"]["should_execute"] == "${{ steps.parse.outputs.should_execute }}"
    parse_text = yaml.safe_dump(parse_job)
    assert "CODEX_API_KEY" not in parse_text
    assert "run-task.py" not in parse_text


def test_execute_requires_should_execute_and_skips_on_parse_failure() -> None:
    payload, _ = _load()
    execute = payload["jobs"]["execute"]
    assert execute["needs"] == ["parse-spec"]
    assert execute["if"] == "${{ needs.parse-spec.outputs.should_execute == 'true' }}"
    assert "always()" not in str(execute.get("if", ""))


def test_execute_uses_task_id_concurrency() -> None:
    payload, _ = _load()
    concurrency = payload["jobs"]["execute"]["concurrency"]
    assert concurrency["group"] == "autonomous-agent-${{ needs.parse-spec.outputs.task_id }}"
    assert concurrency["cancel-in-progress"] is False
    assert "queue" not in concurrency
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Do not re-push the same task_id" in text
    assert "queue: max is not used" in text


def test_permissions_are_explicit_and_read_only() -> None:
    payload, _ = _load()
    assert payload["permissions"] == {"contents": "read"}
    for job in payload["jobs"].values():
        assert job["permissions"] == {"contents": "read"}
        assert "write" not in yaml.safe_dump(job["permissions"])


def test_checkout_fetches_history_without_persisting_credentials() -> None:
    payload, _ = _load()
    for job in payload["jobs"].values():
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["uses"] == "actions/checkout@v7"
        assert checkout["with"]["fetch-depth"] == 0
        assert checkout["with"]["persist-credentials"] is False


def test_codex_secret_is_not_globally_exposed() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count("secrets.CODEX_API_KEY") == 1
    payload, _ = _load()
    assert "env" not in payload
    parse_job = payload["jobs"]["parse-spec"]
    execute = payload["jobs"]["execute"]
    assert "env" not in parse_job
    assert "env" not in execute
    secret_steps = [step for step in execute["steps"] if "CODEX_API_KEY" in yaml.safe_dump(step)]
    assert len(secret_steps) == 1
    assert "run-task.py" in secret_steps[0]["run"]
    assert secret_steps[0]["env"]["CODEX_API_KEY"] == "${{ secrets.CODEX_API_KEY }}"
    assert secret_steps[0]["env"]["GITHUB_TOKEN"] == ""
    setup_dump = yaml.safe_dump([step for step in execute["steps"] if step is not secret_steps[0]])
    assert "CODEX_API_KEY" not in setup_dump
    bootstrap = next(
        step
        for step in execute["steps"]
        if str(step.get("uses", "")).startswith("openai/codex-action@")
    )
    assert "CODEX_API_KEY" not in yaml.safe_dump(bootstrap)
    assert bootstrap["with"]["openai-api-key"] == "unused-bootstrap-placeholder"


def test_codex_action_bootstraps_sandbox_without_replacing_orchestrator() -> None:
    payload, _ = _load()
    steps = payload["jobs"]["execute"]["steps"]
    names_or_uses = [(step.get("name"), step.get("uses"), step.get("run", "")) for step in steps]
    uses = [step.get("uses") for step in steps]
    assert "openai/codex-action@v1" in uses
    bootstrap = next(step for step in steps if step.get("uses") == "openai/codex-action@v1")
    inputs = bootstrap["with"]
    assert inputs["sandbox"] == "workspace-write"
    assert inputs["safety-strategy"] == "drop-sudo"
    assert "prompt" not in inputs
    assert "prompt-file" not in inputs
    assert "permission-profile" not in inputs
    assert "danger-full-access" not in yaml.safe_dump(bootstrap)
    assert inputs["openai-api-key"] == "unused-bootstrap-placeholder"
    assert "secrets." not in str(inputs["openai-api-key"])
    run_task = next(step for step in steps if "run-task.py" in str(step.get("run", "")))
    assert steps.index(bootstrap) < steps.index(run_task)
    assert steps[-1] is run_task
    assert "sudo" not in yaml.safe_dump(run_task)
    assert any("actions/setup-python@" in str(item) for item in uses)
    assert any("actions/setup-node@" in str(item) for item in uses)
    assert any("npm install -g" in run for _, _, run in names_or_uses)
    jobs_text = yaml.safe_dump(payload["jobs"])
    assert "self-hosted" not in jobs_text
    assert "danger-full-access" not in jobs_text


def test_feature_branch_push_is_not_unconditional_intake() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    assert "[skip ci]" not in text
    payload, triggers = _load()
    assert "paths" in triggers["push"]
    assert payload["jobs"]["execute"]["if"] == (
        "${{ needs.parse-spec.outputs.should_execute == 'true' }}"
    )
