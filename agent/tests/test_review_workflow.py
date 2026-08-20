from __future__ import annotations

from pathlib import Path

import yaml
from agent.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-review.yml"
EXECUTE = REPO_ROOT / ".github" / "workflows" / "agent-execute.yml"


def _load(path: Path) -> tuple[dict[object, object], dict[object, object]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    triggers = payload.get("on", payload.get(True))
    assert isinstance(triggers, dict)
    return payload, triggers


def test_review_workflow_uses_async_github_events() -> None:
    payload, triggers = _load(WORKFLOW)
    assert payload["name"] == "Agent Review"
    assert set(triggers) == {"check_run", "status"}
    assert "issue_comment" not in triggers
    assert "pull_request_review" not in triggers
    assert "pull_request_review_comment" not in triggers
    assert triggers["check_run"]["types"] == ["completed"]
    assert triggers["status"] is None or triggers["status"] == {}
    assert "pull_request_target" not in triggers
    assert payload["permissions"] == {"contents": "read"}
    assert "concurrency" not in payload


def test_review_workflow_skips_forks_and_does_not_poll() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    assert "sleep " not in text
    payload, triggers = _load(WORKFLOW)
    assert "pull_request_target" not in triggers
    prepare = payload["jobs"]["prepare"]
    condition = str(prepare["if"])
    assert "check_run" in condition
    assert "pending" in condition
    assert "issue_comment" not in condition
    assert "pull_request_review" not in condition
    assert prepare["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
    }
    assert "checks" not in prepare["permissions"]
    assert "CODEX_API_KEY" not in yaml.safe_dump(prepare)
    assert "REVIEW_CLASSIFIER_API_KEY" not in yaml.safe_dump(prepare)
    assert "concurrency" not in prepare


def test_review_job_checks_out_api_head_and_isolates_secrets() -> None:
    payload, _ = _load(WORKFLOW)
    review = payload["jobs"]["review"]
    assert review["if"] == "${{ needs.prepare.outputs.should_review == 'true' }}"
    group = review["concurrency"]["group"]
    assert "github.workflow" in group
    assert "needs.prepare.outputs.pull_number" in group
    assert review["concurrency"]["cancel-in-progress"] is False
    assert "queue" not in review["concurrency"]
    assert review["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
        "checks": "read",
        "statuses": "read",
    }
    assert review["permissions"].get("checks") == "read"
    assert review["permissions"].get("statuses") == "read"
    assert "write" not in str(review["permissions"].get("checks"))
    assert "write" not in str(review["permissions"].get("statuses"))
    checkouts = [
        step
        for step in review["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 2
    trusted, workspace = checkouts
    assert trusted["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert trusted["with"]["path"] == "_trusted"
    assert trusted["with"]["persist-credentials"] is False
    assert workspace["with"]["ref"] == "${{ needs.prepare.outputs.head_sha }}"
    assert workspace["with"]["persist-credentials"] is False
    assert workspace["with"]["fetch-depth"] == 0
    assert "path" not in workspace["with"]
    install = next(
        step
        for step in review["steps"]
        if "pip install" in str(step.get("run", ""))
    )
    assert "${{ runner.temp }}/orchestrator" in install["run"]
    assert "pip install -e ." not in install["run"].replace(
        "${{ runner.temp }}/orchestrator", ""
    )
    secret_steps = [step for step in review["steps"] if "CODEX_API_KEY" in yaml.safe_dump(step)]
    assert len(secret_steps) == 1
    env = secret_steps[0]["env"]
    assert env["CODEX_API_KEY"] == "${{ secrets.CODEX_API_KEY }}"
    assert env["REVIEW_CLASSIFIER_API_KEY"] == "${{ secrets.REVIEW_CLASSIFIER_API_KEY }}"
    run = secret_steps[0]["run"]
    assert "run-review.py" in run
    assert "${{ runner.temp }}/orchestrator" in run
    assert "--repo-root" in run
    assert "${{ github.workspace }}" in run
    assert "${{ needs.prepare.outputs.head_sha }}" in run
    bootstrap = next(
        step
        for step in review["steps"]
        if str(step.get("uses", "")).startswith("openai/codex-action@")
    )
    inputs = bootstrap["with"]
    assert inputs["openai-api-key"] == "unused-bootstrap-placeholder"
    assert "secrets." not in str(inputs["openai-api-key"])
    assert inputs.get("prompt") in (None, "")
    assert inputs.get("prompt-file") in (None, "")
    assert inputs["allow-bot-users"] == "${{ needs.prepare.outputs.coderabbit_actor }}"
    assert "coderabbitai[bot]" not in str(inputs["allow-bot-users"])
    assert inputs.get("allow-bots") in (None, False, "false")
    assert "merge" not in run.lower()
    prepare = payload["jobs"]["prepare"]
    assert prepare["outputs"]["coderabbit_actor"] == "${{ steps.gate.outputs.coderabbit_actor }}"


def test_prepare_job_loads_trusted_default_branch() -> None:
    payload, _ = _load(WORKFLOW)
    prepare = payload["jobs"]["prepare"]
    checkout = next(
        step
        for step in prepare["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False


def test_coderabbit_yaml_keeps_incremental_review() -> None:
    payload = yaml.safe_load((REPO_ROOT / ".coderabbit.yaml").read_text(encoding="utf-8"))
    reviews = payload["reviews"]
    assert reviews["request_changes_workflow"] is False
    assert reviews["high_level_summary"] is False
    assert reviews["high_level_summary_in_walkthrough"] is True
    auto = reviews["auto_review"]
    assert auto["enabled"] is True
    assert auto["auto_incremental_review"] is True
    pause = auto["auto_pause_after_reviewed_commits"]
    assert pause != 0
    assert pause >= 1 + load_config().retry.review_attempt_limit
    assert auto["drafts"] is False
    branches = auto["base_branches"]
    assert isinstance(branches, list)
    assert "^main$" in branches
    assert "e2e/phase7-.*" in branches
    assert reviews["review_progress"] is True
    assert reviews["review_status"] is True
    touches = reviews["finishing_touches"]
    assert touches["autofix"]["enabled"] is False
    assert touches["docstrings"]["enabled"] is False
    assert touches["unit_tests"]["enabled"] is False
    assert touches["simplify"]["enabled"] is False
    assert touches["fix_ci"]["enabled"] is False
    assert touches["resolve_merge_conflict"]["enabled"] is False


def test_execute_workflow_does_not_wait_for_coderabbit() -> None:
    text = EXECUTE.read_text(encoding="utf-8").lower()
    assert "coderabbit" not in text
    assert "agent-review.yml" not in text
    assert "sleep " not in text
    payload, triggers = _load(EXECUTE)
    assert "pull_request_review" not in triggers
    assert "pull_request_target" not in triggers
