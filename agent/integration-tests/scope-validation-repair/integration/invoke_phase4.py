#!/usr/bin/env python3
"""Test-only adapter that injects a fake executable into the real Phase 4 cycle."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_INVALID = 2
EXIT_POLICY = 3
EXIT_INTERNAL = 4

SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")


def _print_json(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _safe_env() -> dict[str, str]:
    from agent.codex_runner import DENIED_ENV_ALWAYS, ENV_ALLOWLIST

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in DENIED_ENV_ALWAYS:
            continue
        if key == "CODEX_API_KEY" or "API_KEY" in key:
            continue
        if key.endswith(SECRET_SUFFIXES):
            continue
        if key not in ENV_ALLOWLIST:
            continue
        env[key] = value
    python_dir = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if python_dir not in parts:
        env["PATH"] = python_dir + (os.pathsep + path if path else "")
    return env


def _status(result) -> str:
    from agent.classify import FailureClass

    if result.outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"}:
        return "PASS"
    if result.outcome == "SCOPE_VIOLATION":
        return "SCOPE_VIOLATION"
    if result.outcome == "FAILED" and result.classification is FailureClass.ENVIRONMENT_FAILURE:
        return "ENVIRONMENT_FAILURE"
    if result.outcome == "ESCALATED":
        return "ESCALATION_REQUIRED"
    return result.outcome


def _payload(result, *, expected_task: str | None = None) -> dict:
    classification = None if result.classification is None else result.classification.value
    payload = {
        "ok": result.outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"},
        "status": _status(result),
        "repair_attempts": result.repair_attempts,
        "task_id": result.task_id,
        "outcome": result.outcome,
        "classification": classification,
        "violation_paths": [] if result.scope is None else list(result.scope.violation_paths),
    }
    if expected_task is not None:
        payload["expected_task"] = expected_task
    return payload


def _exit_for_result(result) -> int:
    if result.outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"}:
        return EXIT_OK
    if result.outcome == "SCOPE_VIOLATION":
        return EXIT_POLICY
    return EXIT_INVALID


def _exit_for_error(error: BaseException) -> int:
    from agent.errors import AgentError, ErrorCategory, error_category_of

    if isinstance(error, AgentError):
        payload = {"ok": False, **error.to_dict()}
        _print_json(payload)
        if error.category is ErrorCategory.ENVIRONMENT_FAILURE:
            return EXIT_ENVIRONMENT
        if error.category is ErrorCategory.POLICY_VIOLATION:
            return EXIT_POLICY
        if error.category is ErrorCategory.INVALID_INPUT:
            return EXIT_INVALID
        return EXIT_INTERNAL
    _print_json(
        {
            "ok": False,
            "category": error_category_of(error).value,
            "code": "INTERNAL_FAILURE",
            "message": str(error),
        }
    )
    return EXIT_INTERNAL


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--codex-bin", type=Path, required=True)
    args = parser.parse_args()

    source_repo = args.source_repo.resolve()
    if str(source_repo) not in sys.path:
        sys.path.insert(0, str(source_repo))

    from agent.config import load_config
    from agent.cycle import run_task_cycle

    try:
        config = load_config()
        test_config = replace(
            config, codex=replace(config.codex, bin=str(args.codex_bin.resolve()))
        )
        result = run_task_cycle(
            args.spec,
            repo_root=args.repo_root,
            config=test_config,
            env=_safe_env(),
        )
        if result.task_id != args.task:
            _print_json(_payload(result, expected_task=args.task) | {"ok": False})
            return EXIT_INVALID
        _print_json(_payload(result))
        return _exit_for_result(result)
    except Exception as exc:
        return _exit_for_error(exc)


if __name__ == "__main__":
    raise SystemExit(main())
