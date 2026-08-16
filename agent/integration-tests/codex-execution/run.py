#!/usr/bin/env python3
"""Run the real Phase 2/3 CLIs against deterministic Codex execution fixtures."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
    if path.name.endswith(".BLOCKED.md"):
        return "BLOCKED"
    raise ValueError(f"Unknown fixture naming convention: {path.name}")


def invoke(
    python: str,
    repo_root: Path,
    fixture: Path,
    command_name: str,
    arguments: list[str],
    expected: str,
    *,
    environment: dict[str, str],
) -> CommandResult:
    script = repo_root / "agent" / "scripts" / f"{command_name}.py"
    argv = [python, str(script), *arguments]
    completed = subprocess.run(
        argv,
        cwd=repo_root,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return CommandResult(
        file=fixture.name,
        expected=expected,
        command=subprocess.list2cmdline(argv),
        status="OK" if completed.returncode == 0 else "ERROR",
        exit_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def invoke_test_runner(
    python: str,
    repo_root: Path,
    script_dir: Path,
    fixture: Path,
    task: str,
    workspace: Path,
    codex_bin: Path,
    expected: str,
    *,
    environment: dict[str, str],
) -> CommandResult:
    script = script_dir / "invoke_runner.py"
    argv = [
        python,
        str(script),
        "--source-repo",
        str(repo_root),
        "--spec",
        str(fixture.resolve()),
        "--task",
        task,
        "--repo-root",
        str(workspace),
        "--codex-bin",
        str(codex_bin.resolve()),
    ]
    completed = subprocess.run(
        argv,
        cwd=repo_root,
        env=environment,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return CommandResult(
        file=fixture.name,
        expected=expected,
        command=subprocess.list2cmdline(argv),
        status="OK" if completed.returncode == 0 else "ERROR",
        exit_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def assertion_result(
    fixture: Path,
    expected: str,
    errors: list[str],
    observations: dict[str, Any],
) -> CommandResult:
    return CommandResult(
        file=fixture.name,
        expected=expected,
        command="assert-phase3-acceptance",
        status="OK" if not errors else "ERROR",
        exit_code=0 if not errors else 1,
        stdout=json.dumps(observations, ensure_ascii=False, sort_keys=True),
        stderr="\n".join(errors),
    )


def json_stdout(result: CommandResult) -> dict[str, Any]:
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise ValueError("command stdout is not a JSON object")
    return payload


def snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def changed_paths(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def fake_capture(stdout: str) -> dict[str, Any] | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "phase3.fake_codex.capture":
            return event
    return None


def create_fake_launcher(bin_dir: Path, python: str, fake_script: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = bin_dir / "codex.cmd"
        launcher.write_text(
            f'@echo off\r\n"{python}" "{fake_script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(
            "#!/usr/bin/env sh\n"
            f"exec {shlex.quote(python)} {shlex.quote(str(fake_script))} \"$@\"\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
    return launcher


def add_error(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def verify_workspace(
    workspace: Path,
    expected_dir: Path,
    expected_data: dict[str, Any],
    before: dict[str, str],
    errors: list[str],
) -> list[str]:
    after = snapshot(workspace)
    actual_changed = changed_paths(before, after)
    wanted_changed = sorted(expected_data["expected_changed_files"])
    add_error(
        errors,
        actual_changed == wanted_changed,
        f"changed paths mismatch: expected {wanted_changed}, actual {actual_changed}",
    )
    for relative in expected_data["exact_files"]:
        actual = workspace / relative
        expected = expected_dir / "files" / relative
        add_error(errors, actual.is_file(), f"missing workspace file: {relative}")
        add_error(errors, expected.is_file(), f"missing expected file: {expected}")
        if actual.is_file() and expected.is_file():
            add_error(
                errors,
                actual.read_bytes() == expected.read_bytes(),
                f"content mismatch: {relative}",
            )
    for relative in expected_data["unchanged_files"]:
        add_error(
            errors,
            before.get(relative) == after.get(relative),
            f"unexpected change: {relative}",
        )
    for relative in expected_data["absent_files"]:
        add_error(
            errors,
            not (workspace / relative).exists(),
            f"Phase 3 executed a deferred command: {relative}",
        )
    return actual_changed


def run_case(
    *,
    case: dict[str, str],
    all_cases: list[dict[str, str]],
    script_dir: Path,
    suite_temp: Path,
    repo_root: Path,
    python: str,
    environment: dict[str, str],
    real_codex: bool,
    codex_bin: Path | None,
) -> tuple[list[CommandResult], dict[str, Any]]:
    fixture = script_dir / "fixtures" / case["fixture"]
    expected_name = expectation(fixture)
    expected_dir = script_dir / "expected" / case["case"]
    expected_data = json.loads((expected_dir / "assertions.json").read_text(encoding="utf-8"))
    case_temp = suite_temp / case["case"]
    workspace = case_temp / "workspace"
    state_root = case_temp / "state-root"
    shutil.copytree(script_dir / "workspaces" / case["case"], workspace)
    state_root.mkdir(parents=True)
    before = snapshot(workspace)

    rows: list[CommandResult] = []
    errors: list[str] = []
    observations: dict[str, Any] = {
        "case": case["case"],
        "codex": "not-executed",
        "selected_task": None,
        "workspace": "unknown",
    }

    parsed = invoke(
        python,
        repo_root,
        fixture,
        "parse-spec",
        [str(fixture.resolve())],
        expected_name,
        environment=environment,
    )
    rows.append(parsed)
    validated = invoke(
        python,
        repo_root,
        fixture,
        "validate-spec",
        [str(fixture.resolve())],
        expected_name,
        environment=environment,
    )
    rows.append(validated)
    if parsed.exit_code != 0 or validated.exit_code != 0:
        errors.append("real Phase 2 parse/validation did not succeed")
        rows.append(assertion_result(fixture, expected_name, errors, observations))
        return rows, observations

    try:
        spec_id = json_stdout(parsed)["spec"]["id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"parse-spec JSON did not contain spec.id: {exc}")
        rows.append(assertion_result(fixture, expected_name, errors, observations))
        return rows, observations

    initialized = invoke(
        python,
        repo_root,
        fixture,
        "init-state",
        [
            "--spec",
            str(fixture.resolve()),
            "--overwrite",
            "--repo-root",
            str(state_root),
        ],
        expected_name,
        environment=environment,
    )
    rows.append(initialized)
    if initialized.exit_code != 0:
        errors.append("real Phase 2 state initialization did not succeed")
        rows.append(assertion_result(fixture, expected_name, errors, observations))
        return rows, observations

    updated = invoke(
        python,
        repo_root,
        fixture,
        "update-state",
        ["--task-id", spec_id, "--to", "RUNNING", "--repo-root", str(state_root)],
        expected_name,
        environment=environment,
    )
    rows.append(updated)
    selected = invoke(
        python,
        repo_root,
        fixture,
        "select-task",
        ["--spec", str(fixture.resolve()), "--repo-root", str(state_root)],
        expected_name,
        environment=environment,
    )
    rows.append(selected)
    if updated.exit_code != 0 or selected.exit_code != 0:
        errors.append("real Phase 2 state transition/task selection did not succeed")
        rows.append(assertion_result(fixture, expected_name, errors, observations))
        return rows, observations

    try:
        selected_task = json_stdout(selected)["task_id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        selected_task = None
        errors.append(f"select-task JSON did not contain task_id: {exc}")
    observations["selected_task"] = selected_task
    add_error(
        errors,
        selected_task == expected_data["selected_task"],
        "selected task mismatch: "
        f"expected {expected_data['selected_task']}, actual {selected_task}",
    )
    if selected_task is None:
        rows.append(assertion_result(fixture, expected_name, errors, observations))
        return rows, observations

    if real_codex:
        executed = invoke(
            python,
            repo_root,
            fixture,
            "run-codex",
            [
                "--spec",
                str(fixture.resolve()),
                "--task",
                selected_task,
                "--repo-root",
                str(workspace),
            ],
            expected_name,
            environment=environment,
        )
    else:
        if codex_bin is None:
            raise RuntimeError("Fake Codex launcher was not configured")
        executed = invoke_test_runner(
            python,
            repo_root,
            script_dir,
            fixture,
            selected_task,
            workspace,
            codex_bin,
            expected_name,
            environment=environment,
        )
    rows.append(executed)
    observations["codex"] = "executed"
    add_error(errors, executed.exit_code == 0, f"run-codex exit code: {executed.exit_code}")

    result_payload: dict[str, Any] = {}
    try:
        result_payload = json_stdout(executed)
    except (ValueError, json.JSONDecodeError) as exc:
        errors.append(f"run-codex stdout was not result JSON: {exc}")

    metadata = result_payload.get("metadata", {})
    final_response = result_payload.get("final_response") or ""
    add_error(errors, result_payload.get("ok") is True, "run-codex result was not ok")
    add_error(errors, metadata.get("task_id") == selected_task, "Runner task metadata mismatch")
    add_error(errors, metadata.get("spec_id") == spec_id, "Runner spec metadata mismatch")
    add_error(
        errors,
        Path(metadata.get("cwd", "")).resolve() == workspace.resolve(),
        "Runner working directory mismatch",
    )
    argv = metadata.get("argv", [])
    expected_bin = "codex" if real_codex else str(codex_bin.resolve())
    add_error(
        errors,
        len(argv) > 1 and argv[0] == expected_bin and argv[1] == "exec",
        "not the expected codex exec command",
    )
    add_error(errors, metadata.get("sandbox") == "workspace-write", "sandbox mismatch")

    capture = None if real_codex else fake_capture(str(result_payload.get("stdout", "")))
    if not real_codex:
        add_error(errors, capture is not None, "Fake Codex capture event is missing")
    if capture is not None:
        capture_argv = capture.get("argv", [])
        prompt = str(capture.get("prompt", ""))
        env_keys = set(capture.get("environment_keys", []))
        add_error(errors, capture_argv[:1] == ["exec"], "Fake did not receive exec")
        add_error(errors, "--json" in capture_argv, "Fake did not receive --json")
        add_error(errors, capture_argv[-1:] == ["-"], "prompt was not passed on stdin")
        add_error(
            errors,
            Path(str(capture.get("working_directory", ""))).resolve() == workspace.resolve(),
            "Codex process working directory mismatch",
        )
        add_error(errors, case["marker"] in prompt, "Current Task marker is missing from prompt")
        add_error(errors, selected_task in prompt, "selected Task id is missing from prompt")
        add_error(errors, "# Current Task" in prompt, "Current Task prompt section is missing")
        add_error(
            errors,
            "run task Validation or Final Verification commands" in prompt,
            "runtime validation prohibition is missing from prompt",
        )
        other_markers = {item["marker"] for item in all_cases if item != case}
        add_error(
            errors,
            not any(marker in prompt for marker in other_markers),
            "prompt contains a task other than Current Task",
        )
        add_error(errors, "CODEX_API_KEY" in env_keys, "allowlisted synthetic key was missing")
        for denied in ("OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"):
            add_error(
                errors,
                denied not in env_keys,
                f"denied environment key reached Codex: {denied}",
            )
        if "inspection_evidence" in expected_data:
            add_error(
                errors,
                expected_data["inspection_evidence"] in capture.get("inspected_files", []),
                "repository inspection evidence is missing",
            )
        add_error(
            errors,
            sorted(capture.get("changed_files", []))
            == sorted(expected_data["expected_changed_files"]),
            "Fake Codex changed-file report mismatch",
        )

    actual_changed = verify_workspace(workspace, expected_dir, expected_data, before, errors)
    observations["workspace"] = "changed" if actual_changed else "unchanged"
    observations["changed_files"] = actual_changed
    if expected_name == "BLOCKED":
        add_error(errors, "IMPLEMENTATION_BLOCKED" in final_response, "blocked marker missing")
        for field in expected_data["blocked_fields"]:
            add_error(errors, field in final_response, f"blocked response missing {field}")
    elif not real_codex:
        add_error(errors, "CODEX_EXECUTED" in final_response, "execution marker missing")
        expected_event = "WORKSPACE_CHANGED" if actual_changed else "NO_CHANGE"
        add_error(
            errors,
            expected_event in final_response,
            f"final response missing {expected_event}",
        )

    rows.append(assertion_result(fixture, expected_name, errors, observations))
    return rows, observations


def write_reports(rows: list[CommandResult], summary: list[dict[str, Any]], path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    with (path / "results.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {"summary": summary, "commands": [asdict(row) for row in rows]},
            handle,
            ensure_ascii=False,
            indent=2,
        )
    with (path / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CommandResult.__annotations__.keys())
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_repo_root = script_dir.parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=default_repo_root)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--report-dir", type=Path, default=script_dir / "reports")
    parser.add_argument("--case", default=None, help="Run one case id")
    parser.add_argument("--all-cases", action="store_true", help="Run all cases")
    parser.add_argument("--real-codex", action="store_true")
    args = parser.parse_args()

    if args.all_cases and args.case is not None:
        print("Cannot use --all-cases and --case together", file=sys.stderr)
        return 2
    if args.all_cases and not args.real_codex:
        print("--all-cases is only valid with --real-codex", file=sys.stderr)
        return 2
    if args.real_codex and os.environ.get("RUN_CODEX_SMOKE_TEST") != "1":
        print("SKIP: set RUN_CODEX_SMOKE_TEST=1 to enable the Real Codex smoke test")
        return 0
    
    repo_root = args.repo_root.resolve()
    required = [
        repo_root / "agent" / "scripts" / f"{name}.py"
        for name in (
            "parse-spec",
            "validate-spec",
            "init-state",
            "update-state",
            "select-task",
            "run-codex",
        )
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        print("Missing implementation script(s):", file=sys.stderr)
        print("\n".join(missing), file=sys.stderr)
        return 2

    all_cases = json.loads((script_dir / "cases.json").read_text(encoding="utf-8"))
    cases = all_cases
    if args.case is not None:
        cases = [item for item in all_cases if item["case"] == args.case]
    elif args.real_codex and not args.all_cases:
        cases = [item for item in all_cases if item["case"] == "01-create-file"]
    else:
        cases = all_cases
    if not cases:
        print(f"Unknown case: {args.case}", file=sys.stderr)
        return 2

    base_environment = os.environ.copy()
    base_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with tempfile.TemporaryDirectory(prefix="phase3-codex-execution-") as temp_name:
        suite_temp = Path(temp_name)
        environment = base_environment.copy()
        codex_bin: Path | None = None
        if not args.real_codex:
            fake_bin = suite_temp / "fake-bin"
            codex_bin = create_fake_launcher(
                fake_bin,
                args.python,
                script_dir / "fake_codex.py",
            )
            environment["CODEX_API_KEY"] = "fixture-codex-key-not-real"
            environment["OPENAI_API_KEY"] = "fixture-openai-key-must-be-excluded"
            environment["GITHUB_TOKEN"] = "fixture-github-token-must-be-excluded"
            environment["GH_TOKEN"] = "fixture-gh-token-must-be-excluded"

        all_rows: list[CommandResult] = []
        summary: list[dict[str, Any]] = []
        for case in cases:
            rows, observations = run_case(
                case=case,
                all_cases=all_cases,
                script_dir=script_dir,
                suite_temp=suite_temp,
                repo_root=repo_root,
                python=args.python,
                environment=environment,
                real_codex=args.real_codex,
                codex_bin=codex_bin,
            )
            all_rows.extend(rows)
            matched = all(row.exit_code == 0 for row in rows)
            summary.append(
                {
                    "file": case["fixture"],
                    "expected": expectation(script_dir / "fixtures" / case["fixture"]),
                    "matched": matched,
                    **observations,
                }
            )

    print(f"{'RESULT':<8} {'EXPECTED':<9} {'WORKSPACE':<10} FILE")
    print("-" * 90)
    for item in summary:
        result = "OK" if item["matched"] else "FAIL"
        print(f"{result:<8} {item['expected']:<9} {item['workspace']:<10} {item['file']}")
    matched_count = sum(item["matched"] for item in summary)
    print("-" * 90)
    print(f"Matched: {matched_count}/{len(summary)}; Mismatched: {len(summary) - matched_count}")

    print("\nPer-command results")
    print(f"{'STATUS':<9} {'EXIT':<5} {'COMMAND':<24} FILE")
    print("-" * 110)
    for row in all_rows:
        if row.command == "assert-phase3-acceptance":
            command_name = row.command
        else:
            command_name = Path(row.command.split()[1]).stem
        exit_code = "-" if row.exit_code is None else str(row.exit_code)
        print(f"{row.status:<9} {exit_code:<5} {command_name:<24} {row.file}")

    write_reports(all_rows, summary, args.report_dir.resolve())
    print(f"\nReports: {args.report_dir.resolve()}")
    return 0 if matched_count == len(summary) else 1


if __name__ == "__main__":
    raise SystemExit(main())
