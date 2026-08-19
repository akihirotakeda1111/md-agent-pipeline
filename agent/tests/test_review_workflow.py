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
    assert set(triggers) >= {
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
    }
    assert triggers["pull_request_review"]["types"] == ["submitted", "edited"]
    assert triggers["pull_request_review_comment"]["types"] == ["created", "edited"]
    assert triggers["issue_comment"]["types"] == ["created", "edited"]
    assert "pull_request_target" not in triggers
    assert payload["permissions"] == {"contents": "read"}
    assert payload["concurrency"]["group"] == (
        "agent-review-${{ github.event.pull_request.number || github.event.issue.number }}"
    )
    assert payload["concurrency"]["cancel-in-progress"] is False


def test_review_workflow_skips_forks_and_does_not_poll() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    assert "sleep " not in text
    payload, triggers = _load(WORKFLOW)
    assert "pull_request_target" not in triggers
    prepare = payload["jobs"]["prepare"]
    assert "github.repository" in str(prepare["if"])
    assert prepare["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "CODEX_API_KEY" not in yaml.safe_dump(prepare)
    assert "REVIEW_CLASSIFIER_API_KEY" not in yaml.safe_dump(prepare)


def test_review_job_checks_out_api_head_and_isolates_secrets() -> None:
    payload, _ = _load(WORKFLOW)
    review = payload["jobs"]["review"]
    assert review["if"] == "${{ needs.prepare.outputs.should_review == 'true' }}"
    assert review["concurrency"]["group"] == "agent-review-${{ needs.prepare.outputs.pull_number }}"
    assert review["concurrency"]["cancel-in-progress"] is False
    assert review["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "issues": "write",
    }
    checkout = next(
        step
        for step in review["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ needs.prepare.outputs.head_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0
    secret_steps = [step for step in review["steps"] if "CODEX_API_KEY" in yaml.safe_dump(step)]
    assert len(secret_steps) == 1
    env = secret_steps[0]["env"]
    assert env["CODEX_API_KEY"] == "${{ secrets.CODEX_API_KEY }}"
    assert env["REVIEW_CLASSIFIER_API_KEY"] == "${{ secrets.REVIEW_CLASSIFIER_API_KEY }}"
    assert "run-review.py" in secret_steps[0]["run"]
    bootstrap = next(
        step
        for step in review["steps"]
        if str(step.get("uses", "")).startswith("openai/codex-action@")
    )
    assert bootstrap["with"]["openai-api-key"] == "unused-bootstrap-placeholder"
    assert "merge" not in secret_steps[0]["run"].lower()


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
