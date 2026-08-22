#!/usr/bin/env python3
"""Test-only adapter that injects a fake executable into the real Phase 4 cycle."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_INVALID = 2
EXIT_POLICY = 3
EXIT_INTERNAL = 4

SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")


def _print_json(payload: dict) -> None:
    # Single-line so harness last_json() can skip preceding event JSONL.
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


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


def _class_value(result) -> str | None:
    classification = getattr(result, "failure_class", None)
    if classification is None:
        classification = getattr(result, "classification", None)
    if classification is None:
        return None
    return classification.value if hasattr(classification, "value") else str(classification)


def _outcome_value(result) -> str:
    outcome = result.outcome
    return outcome.value if hasattr(outcome, "value") else str(outcome)


PRODUCTION_CYCLE_OUTCOMES = {
    "TASK_COMPLETED",
    "FINAL_VERIFICATION_PASSED",
    "FAILED",
    "ESCALATED",
    "SCOPE_VIOLATION",
}
PRODUCTION_WORK_UNIT_OUTCOMES = {
    "FINAL_VERIFICATION_PASSED",
    "FAILED",
    "ESCALATED",
    "SCOPE_VIOLATION",
    "INVALID_SPEC",
    "COMPLETED",
}


def _status(result) -> str:
    from agent.classify import FailureClass

    class_value = _class_value(result)
    outcome = _outcome_value(result)
    if outcome not in PRODUCTION_CYCLE_OUTCOMES and outcome not in PRODUCTION_WORK_UNIT_OUTCOMES:
        return outcome
    if outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"}:
        return "PASS"
    if outcome == "SCOPE_VIOLATION":
        return "SCOPE_VIOLATION"
    if outcome == "FAILED" and class_value == FailureClass.ENVIRONMENT_FAILURE.value:
        return "ENVIRONMENT_FAILURE"
    if outcome == "ESCALATED":
        return "ESCALATION_REQUIRED"
    return outcome


def _payload(result, *, expected_task: str | None = None) -> dict:
    payload = {
        "ok": _outcome_value(result) in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"},
        "status": _status(result),
        "repair_attempts": result.repair_attempts,
        "task_id": result.task_id,
        "outcome": _outcome_value(result),
        "classification": _class_value(result),
        "violation_paths": [] if result.scope is None else list(result.scope.violation_paths),
    }
    if expected_task is not None:
        payload["expected_task"] = expected_task
    return payload


def _work_unit_task_id(report, expected_task: str | None) -> str | None:
    if expected_task is not None and expected_task in report.completed_tasks:
        return expected_task
    if report.current_task:
        return report.current_task
    if report.completed_tasks:
        return report.completed_tasks[-1]
    return None


def _payload_from_work_unit(report, *, expected_task: str | None = None) -> dict:
    payload = {
        "ok": _outcome_value(report) == "FINAL_VERIFICATION_PASSED",
        "status": _status(report),
        "repair_attempts": report.repair_attempts,
        "task_id": _work_unit_task_id(report, expected_task),
        "outcome": _outcome_value(report),
        "classification": _class_value(report),
        "violation_paths": [],
    }
    if expected_task is not None:
        payload["expected_task"] = expected_task
    return payload


def _exit_for_result(result) -> int:
    outcome = _outcome_value(result)
    if outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"}:
        return EXIT_OK
    if outcome == "SCOPE_VIOLATION":
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
    parser.add_argument(
        "--work-unit",
        action="store_true",
        help="Call production run_work_unit() (Case 08 Final Verification).",
    )
    args = parser.parse_args()

    source_repo = args.source_repo.resolve()
    if str(source_repo) not in sys.path:
        sys.path.insert(0, str(source_repo))

    from agent.config import load_config
    from agent.cycle import run_task_cycle
    from agent.workunit import run_work_unit

    try:
        config = load_config()
        test_config = replace(
            config, codex=replace(config.codex, bin=str(args.codex_bin.resolve()))
        )
        if args.work_unit:
            with tempfile.TemporaryDirectory(prefix="phase4-work-unit-") as report_td:
                report = run_work_unit(
                    args.spec,
                    repo_root=args.repo_root,
                    report_dir=report_td,
                    config=test_config,
                    env=_safe_env(),
                    persist_state=False,
                )
            if args.task not in report.completed_tasks and report.current_task != args.task:
                _print_json(
                    _payload_from_work_unit(report, expected_task=args.task) | {"ok": False}
                )
                return EXIT_INVALID
            _print_json(_payload_from_work_unit(report))
            return _exit_for_result(report)
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
