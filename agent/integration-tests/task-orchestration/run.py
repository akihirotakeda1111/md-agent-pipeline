#!/usr/bin/env python3
"""Run the real Phase 2 CLI commands against every Markdown fixture."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class CommandResult:
    file: str
    expected: str
    command: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str


def expectation(path: Path) -> str:
    if path.name.endswith(".PASS.md"):
        return "PASS"
    if path.name.endswith(".INVALID_SPEC.md"):
        return "INVALID_SPEC"
    raise ValueError(f"Unknown fixture naming convention: {path.name}")


def spec_id(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^id:\s*([^#\r\n]+?)\s*$", text, re.MULTILINE)
    if not match:
        return None
    value = match.group(1).strip().strip("\"'")
    return value or None


def invoke(
    python: str,
    repo_root: Path,
    fixture: Path,
    command_name: str,
    arguments: list[str],
    expected: str,
) -> CommandResult:
    script = repo_root / "agent" / "scripts" / f"{command_name}.py"
    argv = [python, str(script), *arguments]
    completed = subprocess.run(
        argv,
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return CommandResult(
        file=fixture.name,
        expected=expected,
        command=" ".join(argv),
        status="OK" if completed.returncode == 0 else "ERROR",
        exit_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def skipped(fixture: Path, expected: str, command: str, reason: str) -> CommandResult:
    return CommandResult(fixture.name, expected, command, "SKIPPED", None, "", reason)


def selection_assertion(
    fixture: Path, expected: str, result: CommandResult, expected_task: str | None
) -> CommandResult:
    try:
        payload = json.loads(result.stdout)
        actual_task = payload.get("task_id")
        actual_reason = payload.get("reason")
        expected_reason = "ALL_COMPLETED" if expected_task is None else "SELECTED"
        matched = (
            result.exit_code == 0
            and payload.get("ok") is True
            and actual_task == expected_task
            and actual_reason == expected_reason
        )
        detail = (
            f"expected task_id={expected_task!r}, reason={expected_reason}; "
            f"actual task_id={actual_task!r}, reason={actual_reason}"
        )
    except (json.JSONDecodeError, AttributeError) as exc:
        matched = False
        detail = f"select-task did not return the expected JSON object: {exc}"
    return CommandResult(
        fixture.name,
        expected,
        f"assert-selection:{expected_task if expected_task is not None else 'ALL_COMPLETED'}",
        "OK" if matched else "ERROR",
        0 if matched else 1,
        detail,
        "",
    )


def update_with_state(
    python: str,
    repo_root: Path,
    fixture: Path,
    expected: str,
    task_id: str,
    target: str,
    completed: list[str] | None = None,
    current_task: str | None | object = ...,
) -> CommandResult:
    extras: dict[str, object] = {}
    if completed is not None:
        extras["completedTasks"] = completed
    if current_task is not ...:
        extras["currentTask"] = current_task
    arguments = ["--task-id", task_id, "--to", target]
    if extras:
        arguments.extend(["--set-json", json.dumps(extras, separators=(",", ":"))])
    return invoke(python, repo_root, fixture, "update-state", arguments, expected)


def run_dependency_selection_sequence(
    python: str,
    repo_root: Path,
    fixture: Path,
    expected: str,
    spec_arg: str,
    task_id: str,
    initial_selection: CommandResult,
) -> list[CommandResult]:
    rows = [selection_assertion(fixture, expected, initial_selection, "prepare")]
    completed: list[str] = []
    for current, next_task in (
        ("prepare", "implement"),
        ("implement", "verify"),
        ("verify", None),
    ):
        for target, current_value in (
            ("IMPLEMENTING", current),
            ("VALIDATING", ...),
        ):
            transition = update_with_state(
                python,
                repo_root,
                fixture,
                expected,
                task_id,
                target,
                current_task=current_value,
            )
            rows.append(transition)
            if transition.exit_code != 0:
                return rows
        completed.append(current)
        transition = update_with_state(
            python,
            repo_root,
            fixture,
            expected,
            task_id,
            "TASK_COMPLETED",
            completed=completed.copy(),
            current_task=None,
        )
        rows.append(transition)
        if transition.exit_code != 0:
            return rows
        selected = invoke(python, repo_root, fixture, "select-task", ["--spec", spec_arg], expected)
        rows.append(selected)
        rows.append(selection_assertion(fixture, expected, selected, next_task))
        if selected.exit_code != 0:
            return rows
    return rows


def run_fixture(
    python: str,
    repo_root: Path,
    fixture: Path,
    update_to: str,
) -> list[CommandResult]:
    expected = expectation(fixture)
    spec_arg = str(fixture.resolve())
    results = [
        invoke(python, repo_root, fixture, "parse-spec", [spec_arg], expected),
        invoke(python, repo_root, fixture, "validate-spec", [spec_arg], expected),
    ]

    validation_passed = results[-1].exit_code == 0
    remaining = ("init-state", "update-state", "select-task")
    if not validation_passed:
        reason = "validate-spec did not succeed"
        results.extend(skipped(fixture, expected, name, reason) for name in remaining)
        return results

    results.append(
        invoke(
            python,
            repo_root,
            fixture,
            "init-state",
            ["--spec", spec_arg, "--overwrite"],
            expected,
        )
    )
    if results[-1].exit_code != 0:
        reason = "init-state did not succeed"
        results.append(skipped(fixture, expected, "update-state", reason))
        results.append(skipped(fixture, expected, "select-task", reason))
        return results

    task_id = spec_id(fixture)
    if task_id is None:
        results.append(skipped(fixture, expected, "update-state", "Frontmatter id not found"))
    else:
        results.append(
            invoke(
                python,
                repo_root,
                fixture,
                "update-state",
                ["--task-id", task_id, "--to", update_to],
                expected,
            )
        )
    selected = invoke(python, repo_root, fixture, "select-task", ["--spec", spec_arg], expected)
    results.append(selected)
    if fixture.name == "valid-multi-task-dependencies.PASS.md" and task_id is not None:
        results.extend(
            run_dependency_selection_sequence(
                python, repo_root, fixture, expected, spec_arg, task_id, selected
            )
        )
    return results


def fixture_matched(rows: list[CommandResult]) -> bool:
    expected = rows[0].expected
    validate = next(row for row in rows if "validate-spec.py" in row.command)
    if expected == "INVALID_SPEC":
        return validate.exit_code not in (None, 0)
    required = [row for row in rows if row.status != "SKIPPED"]
    return len(required) >= 5 and all(row.exit_code == 0 for row in required)


def write_reports(rows: list[CommandResult], summary: list[dict], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    with (report_dir / "results.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {"summary": summary, "commands": [asdict(row) for row in rows]},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    with (report_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CommandResult.__annotations__.keys())
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parents[2]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=repo_root,
        help="Repository containing agent/scripts",
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=script_dir / "fixtures",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
    )
    parser.add_argument(
        "--update-to",
        default="RUNNING",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=script_dir / "reports",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    fixtures_dir = args.fixtures.resolve()
    required_scripts = [
        repo_root / "agent" / "scripts" / f"{name}.py"
        for name in ("parse-spec", "validate-spec", "init-state", "update-state", "select-task")
    ]
    missing = [str(path) for path in required_scripts if not path.is_file()]
    if missing:
        print("Missing implementation script(s):", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        return 2

    fixtures = sorted(fixtures_dir.glob("*.PASS.md")) + sorted(
        fixtures_dir.glob("*.INVALID_SPEC.md")
    )
    fixtures = sorted(fixtures, key=lambda path: path.name)
    if not fixtures:
        print("No fixtures found.", file=sys.stderr)
        return 2

    all_rows: list[CommandResult] = []
    summary: list[dict] = []
    for fixture in fixtures:
        rows = run_fixture(args.python, repo_root, fixture, args.update_to)
        all_rows.extend(rows)
        summary.append(
            {
                "file": fixture.name,
                "expected": expectation(fixture),
                "matched": fixture_matched(rows),
            }
        )

    print(f"{'RESULT':<8} {'EXPECTED':<12} FILE")
    print("-" * 80)
    for item in summary:
        print(f"{('OK' if item['matched'] else 'FAIL'):<8} {item['expected']:<12} {item['file']}")
    matched = sum(item["matched"] for item in summary)
    print("-" * 80)
    print(f"Matched: {matched}/{len(summary)}; Mismatched: {len(summary) - matched}")

    print("\nPer-command results")
    print(f"{'STATUS':<9} {'EXIT':<5} {'COMMAND':<14} FILE")
    print("-" * 100)
    for row in all_rows:
        if row.command.startswith("assert-selection:") or row.exit_code is None:
            command_name = row.command
        else:
            parts = row.command.split()
            command_name = Path(parts[1]).stem if len(parts) > 1 else row.command
        exit_code = "-" if row.exit_code is None else str(row.exit_code)
        print(f"{row.status:<9} {exit_code:<5} {command_name:<14} {row.file}")

    report_dir = args.report_dir or fixtures_dir / "reports"
    write_reports(all_rows, summary, report_dir.resolve())
    print(f"\nReports: {report_dir.resolve()}")
    return 0 if matched == len(summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
