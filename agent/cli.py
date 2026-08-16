"""CLI entry helpers for Phase 2 scripts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent.codex_runner import resolve_task, run_codex
from agent.config import load_config
from agent.cycle import run_task_cycle
from agent.errors import AgentError, ErrorCategory, error_category_of
from agent.gitutil import capture_snapshot, collect_changes
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

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_INVALID = 2
EXIT_POLICY = 3
EXIT_INTERNAL = 4


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


def run_check_scope(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check working-tree paths against spec scope")
    parser.add_argument("--spec", type=Path, required=True)
    _repo_root_arg(parser)
    args = parser.parse_args(argv)
    try:
        spec = parse_spec(args.spec)
        snapshot = capture_snapshot(args.repo_root)
        changes = collect_changes(args.repo_root, snapshot.base_sha)
        result = check_scope(spec, changes)
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
                "ok": result.outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"},
                **result.to_json_dict(),
            }
        )
        if result.outcome in {"TASK_COMPLETED", "FINAL_VERIFICATION_PASSED"}:
            return EXIT_OK
        if result.outcome == "SCOPE_VIOLATION":
            return EXIT_POLICY
        return EXIT_INVALID
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
