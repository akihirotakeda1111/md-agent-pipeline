"""RF-04: pass full Task Spec semantic context to Classifier and Review Repair."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest
from agent.codex_runner import ProcessResult, run_codex
from agent.config import load_config
from agent.errors import AgentError
from agent.gitwrite import head_sha
from agent.review_classify import (
    CLASSIFY_PROMPT_PATH,
    classify_review_comment,
    load_classification_schema,
)
from agent.review_context import format_review_task_context
from agent.review_policy import AUTO_REPAIR_DEFERRED_REASON, decide_review_policy
from agent.review_prompt import PROMPT_PATH as REPAIR_PROMPT_PATH
from agent.review_prompt import build_review_repair_prompt
from agent.review_types import (
    ClassificationResult,
    ReviewClassification,
    ReviewPolicyAction,
)
from agent.spec import parse_spec
from agent.tests.test_phase7 import (
    FakeGithub,
    _comment,
    _pull,
    _result,
    _run,
)
from agent.validation import run_validation_text

PYTHON_COMMAND = Path(sys.executable).name

MULTI_TASK_SPEC = """---
schema_version: 1
id: review-multi
title: Review Multi
status: PENDING
base_branch: main
target_branch: feature/review
allowed_paths:
  - src/**
forbidden_paths:
  - src/legacy/**
repair_attempt_limit: 1
review_attempt_limit: 3
---

# Objective

Ship parser and report modules.

# Non-Goals

Do not add a web UI.

# Forbidden Actions

Do not weaken tests.

# Architecture Invariants

Keep parser independent of report rendering.

# Tasks

## task-1: Write parser

### Requirement

Parse input files into tokens.

### Acceptance Criteria

- src/parser.py emits tokens.

### Validation

```text
{python_command} check_parser.py
```

## task-2: Write report

depends_on: task-1

### Requirement

Render a summary report from tokens.

### Acceptance Criteria

- src/report.py prints summary.

### Validation

```text
{python_command} check_report.py
```

# Final Verification

```text
{python_command} check_all.py
```
"""

EXAMPLE_SPEC = Path(__file__).resolve().parents[2] / "specs" / "tasks" / "example-task.md"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _multi_repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rf04@example.com")
    _git(repo, "config", "user.name", "RF04")
    spec_dir = repo / "specs" / "tasks"
    spec_dir.mkdir(parents=True)
    (spec_dir / "review-demo.md").write_text(
        MULTI_TASK_SPEC.format(python_command=PYTHON_COMMAND),
        encoding="utf-8",
    )
    src = repo / "src"
    src.mkdir()
    (src / "parser.py").write_text("tokens\n", encoding="utf-8")
    (src / "report.py").write_text("summary\n", encoding="utf-8")
    (repo / "check_parser.py").write_text(
        "from pathlib import Path\n"
        "assert 'tokens' in Path('src/parser.py').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    (repo / "check_report.py").write_text(
        "from pathlib import Path\n"
        "assert 'summary' in Path('src/report.py').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    (repo / "check_all.py").write_text(
        "from pathlib import Path\n"
        "assert 'tokens' in Path('src/parser.py').read_text(encoding='utf-8')\n"
        "assert 'summary' in Path('src/report.py').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    _git(repo, "checkout", "-b", "feature/review")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "HEAD")
    return repo


def _multi_spec(repo: Path):
    from agent.spec import bind_spec_identity

    spec = parse_spec(repo / "specs" / "tasks" / "review-demo.md")
    return bind_spec_identity(spec, repo_root=repo, spec_directory="specs/tasks")


def _classifier_ok(body: dict, key: str) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "classification": "NON_ACTIONABLE",
                            "confidence": 0.9,
                            "reason": "nit",
                            "referencedPaths": [],
                        }
                    )
                },
            }
        ]
    }


def _assert_full_semantic_context(text: str, spec) -> None:
    assert spec.id in text
    assert spec.title in text
    assert spec.objective.strip() in text
    assert spec.non_goals.strip() in text
    assert spec.architecture_invariants.strip() in text
    assert spec.forbidden_actions.strip() in text
    assert spec.final_verification.strip() in text
    positions: list[int] = []
    for task in spec.tasks:
        assert f"- id: {task.id}" in text
        assert task.title in text
        depends = ", ".join(task.depends_on) or "(none)"
        assert f"- depends_on: {depends}" in text
        assert task.requirement.strip() in text
        assert task.acceptance_criteria.strip() in text
        assert task.validation.strip() in text
        positions.append(text.index(f"- id: {task.id}"))
    assert positions == sorted(positions)
    assert len(spec.tasks) >= 2
    assert spec.tasks[0].requirement.strip() in text
    assert spec.tasks[-1].requirement.strip() in text


def test_classifier_system_prompt_limits_responsibility() -> None:
    system = CLASSIFY_PROMPT_PATH.read_text(encoding="utf-8")
    assert "technically correct" in system
    assert "implementation candidate" in system
    assert "whether repair should run" in system
    assert "You have no execution authority" in system
    assert "do not decide" in system.lower() or "Do not decide" in system


def test_classification_schema_and_classifier_signature_are_unchanged() -> None:
    schema = load_classification_schema()
    assert schema["required"] == ["classification", "confidence", "reason", "referencedPaths"]
    assert list(schema["properties"]) == [
        "classification",
        "confidence",
        "reason",
        "referencedPaths",
    ]
    assert "relevantTaskIds" not in schema["properties"]
    assert schema["properties"]["classification"]["enum"] == [
        "ACTIONABLE",
        "NON_ACTIONABLE",
        "OUT_OF_SCOPE",
        "CONFLICTS_WITH_SPEC",
        "UNCERTAIN",
    ]
    parameters = inspect.signature(classify_review_comment).parameters
    assert list(parameters)[:2] == ["item", "spec"]
    assert "relevant_task" not in parameters
    assert "current_task" not in parameters


def test_format_review_task_context_includes_every_task_in_order() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    text = format_review_task_context(spec)
    _assert_full_semantic_context(text, spec)
    source = inspect.getsource(format_review_task_context)
    assert "spec.tasks[: " not in source
    assert "tasks[-1]" not in source
    assert "for " in source and "spec.tasks" in source


def test_classifier_request_includes_full_semantic_context_and_scope() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    captured: dict[str, object] = {}

    def requester(body: dict, key: str) -> dict:
        captured["body"] = body
        captured["key"] = key
        return _classifier_ok(body, key)

    classify_review_comment(
        _comment(path="worker/lease.py", body="Fix lease expiry."),
        spec,
        api_key="review-secret",
        requester=requester,
    )
    body = captured["body"]
    assert isinstance(body, dict)
    system = body["messages"][0]["content"]
    user = body["messages"][1]["content"]
    assert "technically correct" in system
    assert "# Classifier Responsibility" in user
    assert "# Task Scope" in user
    assert "allowed_paths: worker/**, infra/terraform/**" in user
    assert "forbidden_paths:" in user
    assert "Default Deny" in user
    assert "# Repository Protected Paths" not in user
    _assert_full_semantic_context(user, spec)
    assert "Fix lease expiry." in user
    assert body["response_format"]["json_schema"]["schema"]["required"] == [
        "classification",
        "confidence",
        "reason",
        "referencedPaths",
    ]


def test_repair_prompt_uses_all_tasks_and_fact_verification(tmp_path: Path) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    prompt = build_review_repair_prompt(
        spec,
        repo_root=repo,
        base_sha=head_sha(repo),
        accepted=(),
        runtime_policy=load_config().runtime_edit_policy,
    )
    contract = REPAIR_PROMPT_PATH.read_text(encoding="utf-8")
    combined = contract + "\n" + prompt
    assert "# Current Task" not in prompt
    _assert_full_semantic_context(prompt, spec)
    assert spec.non_goals.strip() in prompt
    assert "# Repository Protected Paths" in prompt
    assert "Protected > Forbidden > Allowed" in prompt
    assert "Accepted review comments are repair candidates, not verified facts." in combined
    assert "inspect the current repository" in combined.lower() or "current repository" in combined
    assert "already fixed" in combined
    assert "technically incorrect" in combined
    assert "contradict the Task Spec" in combined or "contradicts the Task Spec" in combined
    assert "conflict with each other" in combined
    assert "Do not guess" in combined
    assert "do not change files" in combined
    assert "REPAIR_BLOCKED" in combined
    assert "Evidence:" in combined
    parameters = inspect.signature(build_review_repair_prompt).parameters
    assert "current_task" not in parameters


def test_task_one_comment_does_not_limit_prompt_to_last_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(path="src/parser.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    seen: dict[str, object] = {}

    def wrapping_run_codex(spec_arg, task, **kwargs):
        seen["task_id"] = task.id
        seen["prompt"] = kwargs.get("prompt")
        return run_codex(spec_arg, task, **kwargs)

    monkeypatch.setattr("agent.review_loop.run_codex", wrapping_run_codex)

    def executor(command, *, cwd, env, timeout, stdin):
        Path(cwd, "src", "parser.py").write_text("tokens\nfixed\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(
            ReviewClassification.ACTIONABLE, paths=("src/parser.py",)
        ),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "REVIEW_FIX_PUSHED"
    prompt = str(seen["prompt"])
    assert seen["task_id"] == "task-2"
    assert "# Current Task" not in prompt
    _assert_full_semantic_context(prompt, spec)


def test_cross_task_comment_includes_every_task(tmp_path: Path) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(
        path="src/parser.py",
        body="parser.py and report.py both mishandle empty input.",
        commit_id=head_sha(repo),
    )
    fake.add_check_run(head_sha=head_sha(repo))
    prompts: list[str] = []

    def executor(command, *, cwd, env, timeout, stdin):
        prompts.append(stdin)
        Path(cwd, "src", "parser.py").write_text("tokens\nfixed\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(
            ReviewClassification.ACTIONABLE, paths=("src/parser.py", "src/report.py")
        ),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "REVIEW_FIX_PUSHED"
    assert len(prompts) == 1
    _assert_full_semantic_context(prompts[0], spec)
    assert "parser.py and report.py both mishandle empty input." in prompts[0]


def test_no_change_repair_does_not_commit_or_push(tmp_path: Path) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(path="src/parser.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    before = head_sha(repo)

    def executor(command, *, cwd, env, timeout, stdin):
        return ProcessResult(0, "REPAIR_BLOCKED\nReason: already fixed\n", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(
            ReviewClassification.ACTIONABLE, paths=("src/parser.py",)
        ),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "REVIEW_FIX_VALIDATION_FAILED"
    assert head_sha(repo) == before
    remote = subprocess.run(
        ["git", "rev-parse", "origin/feature/review"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert remote.stdout.strip() == before


def test_scope_violation_does_not_commit_or_push(tmp_path: Path) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(path="src/parser.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    before = head_sha(repo)

    def executor(command, *, cwd, env, timeout, stdin):
        dest = Path(cwd) / "docs" / "outside.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("nope\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(
            ReviewClassification.ACTIONABLE, paths=("src/parser.py",)
        ),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "REVIEW_SCOPE_VIOLATION"
    assert head_sha(repo) == before


def test_repair_runs_every_task_validation_and_final_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(path="src/parser.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    seen_tasks: list[str] = []
    fv_calls = {"n": 0}

    def wrapping_validation(text, **kwargs):
        seen_tasks.append(str(kwargs.get("task_id") or ""))
        return run_validation_text(text, **kwargs)

    def wrapping_final(*args, **kwargs):
        fv_calls["n"] += 1
        from agent.cycle import run_final_verification

        return run_final_verification(*args, **kwargs)

    monkeypatch.setattr("agent.review_loop.run_validation_text", wrapping_validation)
    monkeypatch.setattr("agent.review_loop.run_final_verification", wrapping_final)

    def executor(command, *, cwd, env, timeout, stdin):
        Path(cwd, "src", "parser.py").write_text("tokens\nfixed\n", encoding="utf-8")
        return ProcessResult(0, "", "")

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(
            ReviewClassification.ACTIONABLE, paths=("src/parser.py",)
        ),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome == "REVIEW_FIX_PUSHED"
    assert seen_tasks == ["task-1", "task-2"]
    assert fv_calls["n"] == 1


def test_orchestrator_does_not_truncate_or_retry_reduced_classifier_context(
    tmp_path: Path,
) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(path="src/parser.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    calls: list[str] = []

    def requester(body: dict, key: str) -> dict:
        user = body["messages"][1]["content"]
        calls.append(user)
        _assert_full_semantic_context(user, spec)
        raise AgentError.environment_failure(
            "classifier API HTTP 400: context_length_exceeded",
            code="CLASSIFIER_API_FAILURE",
        )

    def classifier(item, spec_arg):
        return classify_review_comment(item, spec_arg, api_key="review-secret", requester=requester)

    result = _run(repo, fake, classifier=classifier, auto_repair_enabled=True)
    assert result.outcome in {"FAILED", "ESCALATED"}
    assert len(calls) == 1
    assert "task-1" in calls[0] and "task-2" in calls[0]


def test_orchestrator_does_not_retry_reduced_codex_prompt_after_input_failure(
    tmp_path: Path,
) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    fake = FakeGithub(_pull(repo, spec), files=_spec_files(repo))
    fake.add_review_comment(path="src/parser.py", commit_id=head_sha(repo))
    fake.add_check_run(head_sha=head_sha(repo))
    prompts: list[str] = []

    def executor(command, *, cwd, env, timeout, stdin):
        prompts.append(stdin)
        _assert_full_semantic_context(stdin, spec)
        raise AgentError.environment_failure(
            "codex exec failed: input too large",
            code="CODEX_FAILED",
        )

    result = _run(
        repo,
        fake,
        classifier=lambda item, spec: _result(
            ReviewClassification.ACTIONABLE, paths=("src/parser.py",)
        ),
        executor=executor,
        auto_repair_enabled=True,
    )
    assert result.outcome in {"FAILED", "ESCALATED"}
    assert len(prompts) == 1
    assert "# Current Task" not in prompts[0]


@pytest.mark.parametrize(
    ("paths", "reason_part"),
    [
        (("src/parser.py",), "below confidence threshold"),
        ((), "no referenced paths"),
        (("agent/config.json",), "not a subset of allowed_paths"),
        (("src/legacy/old.py",), "not a subset of allowed_paths"),
        (("docs/guide.md",), "not a subset of allowed_paths"),
    ],
)
def test_actionable_auto_repair_policy_escalates_unsafe_candidates(
    tmp_path: Path, paths: tuple[str, ...], reason_part: str
) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    confidence = 0.2 if reason_part == "below confidence threshold" else 0.93
    decision = decide_review_policy(
        ClassificationResult(
            ReviewClassification.ACTIONABLE,
            confidence,
            "reason",
            paths,
        ),
        spec,
        runtime_policy=load_config().runtime_edit_policy,
        confidence_threshold=0.80,
        auto_repair_enabled=True,
    )
    assert decision.action is ReviewPolicyAction.ESCALATE
    assert reason_part in decision.reason
    assert decision.reason != AUTO_REPAIR_DEFERRED_REASON


def test_auto_repair_disabled_still_defers_actionable(tmp_path: Path) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    decision = decide_review_policy(
        _result(ReviewClassification.ACTIONABLE, paths=("src/parser.py",)),
        spec,
        runtime_policy=load_config().runtime_edit_policy,
        confidence_threshold=0.80,
        auto_repair_enabled=False,
    )
    assert decision.action is ReviewPolicyAction.ESCALATE
    assert decision.reason == AUTO_REPAIR_DEFERRED_REASON


@pytest.mark.parametrize(
    ("label", "action", "reason"),
    [
        (ReviewClassification.NON_ACTIONABLE, ReviewPolicyAction.IGNORE, "non-actionable review"),
        (ReviewClassification.OUT_OF_SCOPE, ReviewPolicyAction.ESCALATE, "out of scope"),
        (
            ReviewClassification.CONFLICTS_WITH_SPEC,
            ReviewPolicyAction.ESCALATE,
            "conflicts with the Task Spec",
        ),
        (ReviewClassification.UNCERTAIN, ReviewPolicyAction.ESCALATE, "uncertain"),
    ],
)
def test_non_actionable_classifications_keep_existing_order(
    tmp_path: Path, label: ReviewClassification, action: ReviewPolicyAction, reason: str
) -> None:
    repo = _multi_repo(tmp_path)
    spec = _multi_spec(repo)
    decision = decide_review_policy(
        _result(label, paths=("src/parser.py",)),
        spec,
        runtime_policy=load_config().runtime_edit_policy,
        confidence_threshold=0.80,
        auto_repair_enabled=True,
    )
    assert decision.action is action
    assert reason in decision.reason


def _spec_files(repo: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in (repo / "specs" / "tasks").rglob("*.md"):
        files[path.relative_to(repo).as_posix()] = path.read_text(encoding="utf-8")
    return files
