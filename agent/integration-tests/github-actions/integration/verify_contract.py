#!/usr/bin/env python3
"""Fail-closed structural checks for production agent-execute.yml."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "agent-execute.yml"
CONCURRENCY_GROUP = "autonomous-agent-${{ needs.parse-spec.outputs.task_id }}"
EXECUTE_IF = "${{ needs.parse-spec.outputs.should_execute == 'true' }}"
CODEX_ACTION = "openai/codex-action@v1"
BOOTSTRAP_PLACEHOLDER = "unused-bootstrap-placeholder"


def _step_index(steps: list[Any], predicate: Callable[[Any], bool]) -> int:
    for index, step in enumerate(steps):
        if predicate(step):
            return index
    return -1


def _uses(step: Any, prefix: str) -> bool:
    return isinstance(step, dict) and str(step.get("uses", "")).startswith(prefix)


def _run_contains(step: Any, needle: str) -> bool:
    return isinstance(step, dict) and needle in str(step.get("run", ""))


def _triggers(doc: dict[str, Any]) -> dict[str, Any]:
    if "on" in doc:
        value = doc["on"]
    elif True in doc:
        value = doc[True]
    else:
        raise ValueError("workflow has no `on` triggers")
    if not isinstance(value, dict):
        raise ValueError("workflow `on` must be a mapping")
    return value


def _require(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def collect_failures(text: str | None = None) -> list[str]:
    raw = WORKFLOW.read_text(encoding="utf-8") if text is None else text
    failures: list[str] = []
    _require(WORKFLOW.is_file() or text is not None, f"missing workflow: {WORKFLOW}", failures)
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return [f"workflow is not valid YAML: {exc}"]
    if not isinstance(loaded, dict):
        return ["workflow root must be a mapping"]

    triggers = _triggers(loaded)
    push = triggers.get("push")
    _require(isinstance(push, dict), "push trigger must be a mapping", failures)
    paths = push.get("paths") if isinstance(push, dict) else None
    _require(
        isinstance(paths, list) and "specs/tasks/**/*.md" in paths,
        "push paths must include specs/tasks/**/*.md",
        failures,
    )
    dispatch = triggers.get("workflow_dispatch")
    _require(isinstance(dispatch, dict), "workflow_dispatch trigger is missing", failures)
    spec_path = dispatch.get("inputs", {}).get("spec_path") if isinstance(dispatch, dict) else None
    _require(
        isinstance(spec_path, dict) and spec_path.get("required") is True,
        "workflow_dispatch must require spec_path",
        failures,
    )
    _require("pull_request" not in triggers, "pull_request trigger is forbidden", failures)
    _require(
        "pull_request_target" not in triggers,
        "pull_request_target trigger is forbidden",
        failures,
    )

    permissions = loaded.get("permissions")
    _require(
        isinstance(permissions, dict) and permissions.get("contents") == "read",
        "workflow permissions.contents must be read",
        failures,
    )
    _require(
        isinstance(permissions, dict) and "write" not in permissions.values(),
        "workflow permissions must not grant write",
        failures,
    )

    jobs = loaded.get("jobs")
    _require(isinstance(jobs, dict), "jobs mapping is missing", failures)
    if not isinstance(jobs, dict):
        return failures

    parse_job = jobs.get("parse-spec")
    execute_job = jobs.get("execute")
    deliver_job = jobs.get("deliver")
    _require(isinstance(parse_job, dict), "parse-spec job is missing", failures)
    _require(isinstance(execute_job, dict), "execute job is missing", failures)
    _require(isinstance(deliver_job, dict), "deliver job is missing", failures)
    if isinstance(parse_job, dict):
        _require(
            parse_job.get("name") == "Parse spec", 'parse-spec name must be "Parse spec"', failures
        )
        _require(
            parse_job.get("permissions", {}).get("contents") == "read",
            "parse-spec permissions.contents must be read",
            failures,
        )
    if isinstance(execute_job, dict):
        _require(
            execute_job.get("name") == "Execute task",
            'execute name must be "Execute task"',
            failures,
        )
        _require(
            execute_job.get("permissions", {}).get("contents") == "read",
            "execute permissions.contents must be read",
            failures,
        )
        _require(
            execute_job.get("if") == EXECUTE_IF,
            "execute if must be should_execute == true only",
            failures,
        )
        _require(
            "always()" not in str(execute_job.get("if", "")),
            "execute must not use if: always()",
            failures,
        )
        concurrency = execute_job.get("concurrency")
        _require(isinstance(concurrency, dict), "execute concurrency is missing", failures)
        if isinstance(concurrency, dict):
            _require(
                concurrency.get("group") == CONCURRENCY_GROUP,
                "execute concurrency group must be task_id scoped",
                failures,
            )
            _require(
                concurrency.get("cancel-in-progress") is False,
                "running task must not be cancelled",
                failures,
            )

    if isinstance(deliver_job, dict):
        _require(
            deliver_job.get("permissions", {}).get("contents") == "write",
            "deliver permissions.contents must be write",
            failures,
        )
        _require(
            deliver_job.get("permissions", {}).get("pull-requests") == "write",
            "deliver permissions.pull-requests must be write",
            failures,
        )
        _require(
            deliver_job.get("permissions", {}).get("issues") == "write",
            "deliver permissions.issues must be write",
            failures,
        )
        _require(
            "CODEX_API_KEY" not in yaml.safe_dump(deliver_job),
            "deliver must not receive CODEX_API_KEY",
            failures,
        )
        _require(
            "agent/scripts/deliver.py" in yaml.safe_dump(deliver_job),
            "deliver must call deliver.py",
            failures,
        )
        deliver_run = next(
            (
                step
                for step in deliver_job.get("steps") or []
                if _run_contains(step, "agent/scripts/deliver.py")
            ),
            None,
        )
        _require(isinstance(deliver_run, dict), "deliver.py step is missing", failures)
        if isinstance(deliver_run, dict):
            env = deliver_run.get("env") or {}
            _require(
                env.get("GITHUB_TOKEN") == "${{ github.token }}",
                "deliver must keep GITHUB_TOKEN for non-create GitHub operations",
                failures,
            )
            _require(
                env.get("AGENT_PR_PAT") == "${{ secrets.AGENT_PR_PAT }}",
                "deliver create_pull must use secrets.AGENT_PR_PAT",
                failures,
            )
        if isinstance(execute_job, dict):
            _require(
                "AGENT_PR_PAT" not in yaml.safe_dump(execute_job),
                "execute must not receive AGENT_PR_PAT",
                failures,
            )
        if isinstance(parse_job, dict):
            _require(
                "AGENT_PR_PAT" not in yaml.safe_dump(parse_job),
                "parse-spec must not receive AGENT_PR_PAT",
                failures,
            )
        checkout = next(
            (
                step
                for step in deliver_job.get("steps") or []
                if isinstance(step, dict)
                and str(step.get("uses", "")).startswith("actions/checkout@")
            ),
            None,
        )
        _require(isinstance(checkout, dict), "deliver must checkout", failures)
        if isinstance(checkout, dict):
            _require(
                checkout.get("with", {}).get("persist-credentials") is False,
                "deliver checkout must not persist credentials; push injects auth in-process",
                failures,
            )

    _require("contents: write" in raw, "deliver job must request contents: write", failures)
    _require(
        "pull-requests: write" in raw, "deliver job must request pull-requests: write", failures
    )
    _require("fetch-depth: 0" in raw, "checkout must use fetch-depth: 0", failures)
    _require(
        "persist-credentials: false" in raw,
        "checkout must disable persisted credentials",
        failures,
    )
    _require(
        raw.count("actions/checkout@v7") >= 3,
        "parse, execute, and deliver must use actions/checkout@v7",
        failures,
    )
    _require('python-version: "3.11"' in raw, "setup-python must pin 3.11", failures)
    _require(
        "agent/scripts/prepare-intake.py" in raw, "parse-spec must call prepare-intake.py", failures
    )
    _require(
        "agent/scripts/prepare-execute.py" in raw,
        "execute must call prepare-execute.py",
        failures,
    )
    _require(
        "agent/scripts/run-work-unit.py" in raw, "execute must call run-work-unit.py", failures
    )
    _require("[skip ci]" not in raw.lower(), "workflow must not depend on [skip ci]", failures)
    _require(
        raw.count("CODEX_API_KEY:") == 1,
        "CODEX_API_KEY must occur in exactly one step env",
        failures,
    )
    _require(
        "secrets.CODEX_API_KEY" in raw, "Codex key must come from secrets.CODEX_API_KEY", failures
    )
    _require('GITHUB_TOKEN: ""' in raw, "Orchestrator step must clear GITHUB_TOKEN", failures)
    _require(
        "PHASE5_FAKE_CODEX_API_KEY" not in raw, "test-only fake secret must not appear", failures
    )
    jobs_text = yaml.safe_dump(loaded.get("jobs"))
    _require("danger-full-access" not in jobs_text, "danger-full-access is forbidden", failures)
    _require("self-hosted" not in jobs_text, "self-hosted runners are not used", failures)
    _require(CODEX_ACTION in raw, f"execute must bootstrap with {CODEX_ACTION}", failures)

    if isinstance(execute_job, dict):
        steps = execute_job.get("steps")
        _require(isinstance(steps, list) and steps, "execute steps are missing", failures)
        if isinstance(steps, list) and steps:
            checkout_i = _step_index(steps, lambda step: _uses(step, "actions/checkout@"))
            python_i = _step_index(steps, lambda step: _uses(step, "actions/setup-python@"))
            install_orch_i = _step_index(
                steps, lambda step: _run_contains(step, "pip install -e .")
            )
            prepare_i = _step_index(
                steps, lambda step: _run_contains(step, "agent/scripts/prepare-execute.py")
            )
            node_i = _step_index(steps, lambda step: _uses(step, "actions/setup-node@"))
            install_cli_i = _step_index(steps, lambda step: _run_contains(step, "npm install -g"))
            bootstrap_i = _step_index(steps, lambda step: _uses(step, "openai/codex-action@"))
            run_i = _step_index(
                steps, lambda step: _run_contains(step, "agent/scripts/run-work-unit.py")
            )
            upload_i = _step_index(steps, lambda step: _uses(step, "actions/upload-artifact@"))
            _require(checkout_i >= 0, "execute must checkout first", failures)
            _require(
                checkout_i
                < python_i
                < install_orch_i
                < node_i
                < install_cli_i
                < bootstrap_i
                < run_i
                < upload_i,
                "execute setup must be checkout, toolchain, installs, "
                "Codex bootstrap, orchestrator, artifact upload",
                failures,
            )
            _require(
                prepare_i > install_orch_i and prepare_i < bootstrap_i,
                "prepare-execute.py must run before Codex bootstrap",
                failures,
            )
            _require(
                upload_i == len(steps) - 1,
                "upload-artifact must be the last execute step",
                failures,
            )
            for step in steps[bootstrap_i + 1 :]:
                _require(
                    "sudo" not in yaml.safe_dump(step),
                    "sudo must not be required after Codex bootstrap",
                    failures,
                )

            bootstrap = steps[bootstrap_i] if bootstrap_i >= 0 else None
            if isinstance(bootstrap, dict):
                _require(
                    bootstrap.get("uses") == CODEX_ACTION,
                    f"Codex bootstrap must use {CODEX_ACTION}",
                    failures,
                )
                inputs = bootstrap.get("with")
                _require(isinstance(inputs, dict), "Codex bootstrap inputs are missing", failures)
                if isinstance(inputs, dict):
                    _require(
                        inputs.get("sandbox") == "workspace-write",
                        "Codex bootstrap sandbox must be workspace-write",
                        failures,
                    )
                    _require(
                        inputs.get("safety-strategy") == "drop-sudo",
                        "Codex bootstrap safety-strategy must be drop-sudo",
                        failures,
                    )
                    _require(
                        not inputs.get("prompt") and not inputs.get("prompt-file"),
                        "Codex bootstrap must omit prompt so it does not replace the Orchestrator",
                        failures,
                    )
                    _require(
                        not inputs.get("permission-profile"),
                        "Codex bootstrap must keep legacy workspace-write, not permission-profile",
                        failures,
                    )
                    api_key = inputs.get("openai-api-key")
                    _require(
                        api_key == BOOTSTRAP_PLACEHOLDER,
                        "Codex bootstrap must use a non-secret placeholder, not CODEX_API_KEY",
                        failures,
                    )
                    _require(
                        "secrets." not in str(api_key),
                        "Codex bootstrap must not receive repository secrets",
                        failures,
                    )
                    _require(
                        "steps.codex-cli.outputs.version" in str(inputs.get("codex-version", "")),
                        "Codex bootstrap must pin the same CLI version as agent/config.json",
                        failures,
                    )
    for token in ("git commit", "git push", "gh pr create"):
        _require(token not in raw, f"Phase 6 operation is forbidden: {token}", failures)
    _require(
        re.search(r"invoke_phase5|phase5-integration", raw) is None,
        "production workflow must not reference the test harness",
        failures,
    )
    return failures


def main() -> int:
    if not WORKFLOW.is_file():
        print(f"FAIL: missing {WORKFLOW}", file=sys.stderr)
        return 1
    failures = collect_failures()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        return 1
    print(f"Phase 6 production workflow contract: PASS ({WORKFLOW.as_posix()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
