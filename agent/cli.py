"""CLI entry helpers for Phase 2 scripts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent.codex_runner import resolve_task, run_codex
from agent.config import load_config
from agent.cycle import CycleOutcome, run_task_cycle
from agent.delivery import DeliveryOutcome, run_delivery
from agent.errors import AgentError, ErrorCategory, error_category_of
from agent.gitutil import capture_snapshot, collect_changes
from agent.intake import evaluate_intake, prepare_execute, write_github_output
from agent.review_types import ReviewOutcome
from agent.scope import check_scope
from agent.select import select_next_task
from agent.spec import parse_spec, spec_to_dict
from agent.state import (
    ExecutionStatus,
    apply_transition,
    init_state,
    read_state,
    state_file_path,
    write_state,
)
from agent.validation import run_validation_text
from agent.workunit import WorkUnitOutcome, run_work_unit

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_INVALID = 2
EXIT_POLICY = 3
EXIT_INTERNAL = 4

CYCLE_EXIT_CODES = {
    CycleOutcome.TASK_COMPLETED: EXIT_OK,
    CycleOutcome.FINAL_VERIFICATION_PASSED: EXIT_OK,
    CycleOutcome.SCOPE_VIOLATION: EXIT_POLICY,
    CycleOutcome.FAILED: EXIT_INVALID,
    CycleOutcome.ESCALATED: EXIT_INVALID,
}
WORK_UNIT_EXIT_CODES = {
    WorkUnitOutcome.FINAL_VERIFICATION_PASSED: EXIT_OK,
    WorkUnitOutcome.SCOPE_VIOLATION: EXIT_POLICY,
    WorkUnitOutcome.FAILED: EXIT_ENVIRONMENT,
    WorkUnitOutcome.ESCALATED: EXIT_INVALID,
    WorkUnitOutcome.INVALID_SPEC: EXIT_INVALID,
    WorkUnitOutcome.COMPLETED: EXIT_INVALID,
}
DELIVERY_EXIT_CODES = {
    DeliveryOutcome.PR_CREATED: EXIT_OK,
    DeliveryOutcome.FAILED: EXIT_ENVIRONMENT,
    DeliveryOutcome.ESCALATED: EXIT_POLICY,
}
REVIEW_EXIT_CODES = {
    ReviewOutcome.IN_REVIEW: EXIT_OK,
    ReviewOutcome.REVIEW_FIX_PUSHED: EXIT_OK,
    ReviewOutcome.READY_FOR_HUMAN: EXIT_OK,
    ReviewOutcome.FAILED: EXIT_ENVIRONMENT,
    ReviewOutcome.ESCALATED: EXIT_POLICY,
}


def _print_json(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _exit_for_error(error: BaseException) -> int:
    if isinstance(error, AgentError):
        payload = {"ok": False, **error.to_dict()}
        if error.code is None and error.category is ErrorCategory.INVALID_INPUT:
            payload["code"] = "INVALID_INPUT"
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


def _repo_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root used to resolve relative state paths",
    )


def run_parse_spec(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parse a Task Spec to JSON")
    parser.add_argument("spec_path", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec_path)
        _print_json({"ok": True, "spec": spec_to_dict(spec)})
        return EXIT_OK
    except Exception as exc:
        return _exit_for_error(exc)


def run_validate_spec(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a Task Spec")
    parser.add_argument("spec_path", type=Path)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec_path)
        _print_json({"ok": True, "id": spec.id, "task_count": len(spec.tasks)})
        return EXIT_OK
    except Exception as exc:
        return _exit_for_error(exc)


def run_init_state(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create Execution State JSON for a spec")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing state file (default: refuse)",
    )
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec)
        state = init_state(spec, args.repo_root, overwrite=args.overwrite)
        path = state_file_path(args.repo_root, spec.id)
        _print_json({"ok": True, "path": str(path), "state": state.to_json_dict()})
        return EXIT_OK
    except Exception as exc:
        return _exit_for_error(exc)


def run_update_state(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply an explicit state-machine transition")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--to", required=True, help="Target execution state")
    parser.add_argument(
        "--set-json",
        default=None,
        help="JSON object of extra Execution State fields to set",
    )
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        target = ExecutionStatus(args.to)
        extras = _parse_set_json(args.set_json)
        path = state_file_path(args.repo_root, args.task_id)
        current = read_state(path)
        if current.task_id != args.task_id:
            raise AgentError.invalid_input(
                f"state taskId {current.task_id} does not match --task-id {args.task_id}"
            )
        updated = apply_transition(current, target, **extras)
        write_state(path, updated)
        _print_json({"ok": True, "path": str(path), "state": updated.to_json_dict()})
        return EXIT_OK
    except ValueError as exc:
        return _exit_for_error(AgentError.invalid_input(str(exc)))
    except Exception as exc:
        return _exit_for_error(exc)


def run_select_task(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select the next incomplete spec task")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument(
        "--task-id",
        default=None,
        help="Execution state task id (defaults to spec id)",
    )
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec)
        task_id = args.task_id or spec.id
        state = read_state(state_file_path(args.repo_root, task_id))
        selected = select_next_task(spec, state)
        _print_json(
            {
                "ok": True,
                "task_id": None if selected is None else selected.id,
                "title": None if selected is None else selected.title,
                "reason": "ALL_COMPLETED" if selected is None else "SELECTED",
            }
        )
        return EXIT_OK
    except Exception as exc:
        return _exit_for_error(exc)


def run_codex_exec(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run official Codex CLI as a restricted implementation engine"
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task", required=True, help="Current spec task id")
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec)
        task = resolve_task(spec, args.task)
        result = run_codex(spec, task, repo_root=args.repo_root)
        _print_json({"ok": result.exit_code == 0, **result.to_json_dict()})
        return result.exit_code
    except Exception as exc:
        return _exit_for_error(exc)


def run_prepare_intake(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse Task Spec intake for GitHub Actions without running Codex"
    )
    _repo_root_arg(parser)
    parser.add_argument("--event-name", default=None)
    parser.add_argument("--ref-name", default=None)
    parser.add_argument("--sha", default=None)
    parser.add_argument("--before", default=None)
    parser.add_argument("--spec-path", default=None)
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Append GITHUB_OUTPUT name=value lines (official environment file)",
    )
    args = parser.parse_args(argv)
    try:
        result = evaluate_intake(
            repo_root=args.repo_root,
            event_name=_env_or_arg(args.event_name, "GITHUB_EVENT_NAME"),
            ref_name=_env_or_arg(args.ref_name, "GITHUB_REF_NAME"),
            sha=_env_or_arg(args.sha, "GITHUB_SHA"),
            before_sha=_optional_env_or_arg(args.before, "EVENT_BEFORE", "GITHUB_EVENT_BEFORE"),
            spec_path=_optional_env_or_arg(args.spec_path, "SPEC_PATH", "INPUT_SPEC_PATH"),
        )
        output_file = args.github_output
        if output_file is None and "GITHUB_OUTPUT" in os.environ:
            output_file = Path(os.environ["GITHUB_OUTPUT"])
        if output_file is not None:
            write_github_output(output_file, result.to_output_map())
        _print_json(result.to_json_dict())
        return EXIT_OK if result.valid else EXIT_INVALID
    except Exception as exc:
        return _exit_for_error(exc)


def run_prepare_execute(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assert Git history and execution-state guard before Codex"
    )
    parser.add_argument("--spec", type=Path, required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = prepare_execute(args.spec, repo_root=args.repo_root)
        _print_json(
            {
                "ok": True,
                "spec_id": spec.id,
                "base_branch": spec.base_branch,
                "target_branch": spec.target_branch,
            }
        )
        return EXIT_OK
    except Exception as exc:
        return _exit_for_error(exc)


def _env_or_arg(explicit: str | None, *env_names: str) -> str:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for name in env_names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    raise AgentError.invalid_input(f"missing required value: {env_names[0]}")


def _optional_env_or_arg(explicit: str | None, *env_names: str) -> str | None:
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for name in env_names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def run_check_scope(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check working-tree paths against spec scope")
    parser.add_argument("--spec", type=Path, required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec)
        snapshot = capture_snapshot(args.repo_root)
        changes = collect_changes(args.repo_root, snapshot.base_sha)
        cfg = load_config()
        result = check_scope(spec, changes, cfg.runtime_edit_policy)
        _print_json({"ok": result.allowed, "base_sha": snapshot.base_sha, **result.to_json_dict()})
        return EXIT_OK if result.allowed else EXIT_POLICY
    except Exception as exc:
        return _exit_for_error(exc)


def run_validation(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Task Spec validation commands")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--task", required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec)
        task = resolve_task(spec, args.task)
        cfg = load_config()
        records = run_validation_text(
            task.validation,
            repo_root=args.repo_root,
            task_id=task.id,
            timeout_seconds=cfg.validation.timeout_seconds,
        )
        passed = all(record.passed for record in records)
        _print_json({"ok": passed, "records": [record.to_json_dict() for record in records]})
        return EXIT_OK if passed else EXIT_INVALID
    except Exception as exc:
        return _exit_for_error(exc)


def run_task(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one local task cycle: Codex, scope, validation, repair"
    )
    parser.add_argument("--spec", type=Path, required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        result = run_task_cycle(args.spec, repo_root=args.repo_root)
        _print_json(
            {
                "ok": result.outcome
                in {CycleOutcome.TASK_COMPLETED, CycleOutcome.FINAL_VERIFICATION_PASSED},
                **result.to_json_dict(),
            }
        )
        return CYCLE_EXIT_CODES[result.outcome]
    except Exception as exc:
        return _exit_for_error(exc)


def run_work_unit_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run every remaining task plus final verification without Git writes"
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        report = run_work_unit(args.spec, repo_root=args.repo_root, report_dir=args.report_dir)
        _print_json(
            {
                "ok": report.outcome is WorkUnitOutcome.FINAL_VERIFICATION_PASSED,
                **report.to_json_dict(),
            }
        )
        return WORK_UNIT_EXIT_CODES[report.outcome]
    except Exception as exc:
        return _exit_for_error(exc)


def run_deliver_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Commit, push, and open a pull request from a work-unit report"
    )
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        result = run_delivery(args.spec, repo_root=args.repo_root, report_dir=args.report_dir)
        _print_json({"ok": result.outcome is DeliveryOutcome.PR_CREATED, **result.to_json_dict()})
        return DELIVERY_EXIT_CODES[result.outcome]
    except Exception as exc:
        return _exit_for_error(exc)


def run_prepare_review_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gate a CodeRabbit GitHub event before the review job"
    )
    _repo_root_arg(parser)
    parser.add_argument("--event-path", type=Path, default=None)
    parser.add_argument("--repository", default=None)
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Append GITHUB_OUTPUT name=value lines",
    )
    args = parser.parse_args(argv)
    try:
        event_path = args.event_path
        if event_path is None:
            raw_path = os.environ.get("GITHUB_EVENT_PATH")
            if not raw_path:
                raise AgentError.invalid_input("GITHUB_EVENT_PATH is required")
            event_path = Path(raw_path)
        from agent.review_prepare import load_event_payload, prepare_review

        repository = _env_or_arg(args.repository, "GITHUB_REPOSITORY")
        result = prepare_review(
            repo_root=args.repo_root,
            event_payload=load_event_payload(event_path),
            repository=repository,
        )
        output_file = args.github_output
        if output_file is None and "GITHUB_OUTPUT" in os.environ:
            output_file = Path(os.environ["GITHUB_OUTPUT"])
        if output_file is not None:
            write_github_output(output_file, result.to_output_map())
        _print_json(result.to_json_dict())
        return EXIT_OK
    except Exception as exc:
        return _exit_for_error(exc)


def run_review_cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify CodeRabbit feedback and optionally run a bounded review repair"
    )
    parser.add_argument("--pull-number", type=int, required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--spec", type=Path, default=None)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        from agent.review_loop import run_review

        spec_path = None if args.spec is None else str(args.spec)
        result = run_review(
            repo_root=args.repo_root,
            pull_number=args.pull_number,
            head_sha_expected=args.head_sha,
            spec_path=spec_path,
        )
        success = {
            ReviewOutcome.READY_FOR_HUMAN,
            ReviewOutcome.REVIEW_FIX_PUSHED,
            ReviewOutcome.IN_REVIEW,
        }
        _print_json(
            {
                "ok": result.outcome in success,
                **result.to_json_dict(),
            }
        )
        return REVIEW_EXIT_CODES[result.outcome]
    except Exception as exc:
        return _exit_for_error(exc)


def _parse_set_json(raw: str | None) -> dict[str, Any]:
    if raw is None:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AgentError.invalid_input("invalid --set-json") from exc
    if not isinstance(payload, dict):
        raise AgentError.invalid_input("--set-json must be an object")

    mapping = {
        "currentTask": "current_task",
        "completedTasks": "completed_tasks",
        "repairAttempts": "repair_attempts",
        "reviewAttempts": "review_attempts",
        "lastValidation": "last_validation",
        "lastResult": "last_result",
        "branch": "branch",
        "pullRequest": "pull_request",
    }
    extras: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in mapping:
            raise AgentError.invalid_input(f"unsupported --set-json field: {key}")
        extras[mapping[key]] = value
    return extras
