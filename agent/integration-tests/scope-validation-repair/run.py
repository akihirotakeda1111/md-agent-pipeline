#!/usr/bin/env python3
"""Execute the production Phase 2-4 pipeline and assert Phase 4 acceptance traces."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SECRET_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIAL")
UNOBSERVED_PREFIXES = (".git/", ".agent/state/")
UNOBSERVED_NAMES = {".git", ".agent/state"}


def snap(root):
    observed = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in UNOBSERVED_NAMES or rel.startswith(UNOBSERVED_PREFIXES):
            continue
        observed[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return observed


def changes(a, b):
    return sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))


def last_json(text):
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise ValueError("no JSON object in stdout")


def harness_env():
    env = {}
    allow = {
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "USER",
        "USERNAME",
        "LOGNAME",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
    for key, value in os.environ.items():
        if key not in allow:
            continue
        if key == "CODEX_API_KEY" or "API_KEY" in key:
            continue
        if key.endswith(SECRET_SUFFIXES):
            continue
        env[key] = value
    python_dir = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    parts = path.split(os.pathsep) if path else []
    if python_dir not in parts:
        env["PATH"] = python_dir + (os.pathsep + path if path else "")
    return env


def call(argv, cwd, env):
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def launcher(directory, python, fake):
    directory.mkdir()
    p = directory / ("codex.cmd" if os.name == "nt" else "codex")
    p.write_text(
        (f'@echo off\r\n"{python}" "{fake}" %*\r\n')
        if os.name == "nt"
        else f'#!/bin/sh\nexec "{python}" "{fake}" "$@"\n',
        encoding="utf-8",
    )
    if os.name != "nt":
        p.chmod(0o755)
    return p


def git_baseline(workspace, env):
    def git(*args):
        completed = call(["git", *args], workspace, env)
        if completed.returncode:
            raise RuntimeError(f"git {args[0]} failed: {completed.stderr}")
        return completed

    git("init", "-b", "main")
    git("config", "user.email", "phase4-integration@example.com")
    git("config", "user.name", "phase4-integration")
    git("add", "-A")
    git("commit", "--no-gpg-sign", "-m", "baseline")


def select_task(repo, spec, state, env):
    scripts = repo / "agent" / "scripts"
    py = sys.executable
    parsed = call([py, str(scripts / "parse-spec.py"), str(spec)], repo, env)
    if parsed.returncode:
        raise RuntimeError(f"parse-spec.py failed: {parsed.stderr}")
    spec_id = last_json(parsed.stdout)["spec"]["id"]
    steps = [
        ("validate-spec.py", [str(spec)]),
        ("init-state.py", ["--spec", str(spec), "--overwrite", "--repo-root", str(state)]),
        ("update-state.py", ["--task-id", spec_id, "--to", "RUNNING", "--repo-root", str(state)]),
        ("select-task.py", ["--spec", str(spec), "--repo-root", str(state)]),
    ]
    selected = None
    for script, args in steps:
        r = call([py, str(scripts / script), *args], repo, env)
        if r.returncode:
            raise RuntimeError(f"{script} failed: {r.stderr}")
        if script == "select-task.py":
            selected = last_json(r.stdout).get("task_id") or last_json(r.stdout).get("id")
    if not selected:
        raise RuntimeError("Task Selector did not return task_id/id")
    return selected


def assert_case(actual, expected, before, after, workspace, selected):
    errors = []
    for key in ("status", "repair_attempts"):
        if actual.get(key) != expected.get(key):
            errors.append(f"{key}: expected {expected.get(key)!r}, actual {actual.get(key)!r}")
    if selected != expected["selected_task"]:
        errors.append(f"selected_task: expected {expected['selected_task']!r}, actual {selected!r}")
    if actual.get("task_id") != expected["selected_task"]:
        errors.append(
            "cycle task_id: expected "
            f"{expected['selected_task']!r}, actual {actual.get('task_id')!r}"
        )
    changed = changes(before, after)
    if changed != expected["changed_paths"]:
        errors.append(f"changed_paths: expected {expected['changed_paths']}, actual {changed}")
    if "violation_paths" in expected:
        actual_violations = actual.get("violation_paths")
        if actual_violations != expected["violation_paths"]:
            errors.append(
                "violation_paths: expected "
                f"{expected['violation_paths']}, actual {actual_violations}"
            )
    for absent in expected.get("absent_files", []):
        if (workspace / absent).exists():
            errors.append(f"file must be absent: {absent}")
    return errors, changed


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--case")
    a = p.parse_args()
    here = Path(__file__).resolve().parent
    cases = json.loads((here / "cases.json").read_text(encoding="utf-8"))
    cases = [c for c in cases if not a.case or c["case"] == a.case]
    reports = []
    env = harness_env()
    with tempfile.TemporaryDirectory(prefix="phase4-integration-") as td:
        temp = Path(td)
        codex = launcher(temp / "bin", sys.executable, here / "fake_codex.py")
        for c in cases:
            if c.get("deferred"):
                reports.append(
                    {
                        "case": c["case"],
                        "expected": c["expected"],
                        "status": "DEFERRED",
                        "changed_paths": [],
                        "errors": ["deferred: multi-cycle Final Verification"],
                    }
                )
                print(f"DEFERRED {c['case']}")
                continue
            case = temp / c["case"]
            workspace = case / "workspace"
            state = case / "state"
            state.mkdir(parents=True)
            shutil.copytree(here / "workspaces" / c["case"], workspace)
            git_baseline(workspace, env)
            spec = here / "fixtures" / c["fixture"]
            expected = json.loads(
                (here / "expected" / c["case"] / "assertions.json").read_text(encoding="utf-8")
            )
            before = snap(workspace)
            try:
                task = select_task(a.repo_root.resolve(), spec, state, env)
                invoke = [
                    sys.executable,
                    str(here / "integration" / "invoke_phase4.py"),
                    "--source-repo",
                    str(a.repo_root.resolve()),
                    "--spec",
                    str(spec),
                    "--task",
                    task,
                    "--repo-root",
                    str(workspace),
                    "--codex-bin",
                    str(codex),
                ]
                if c.get("work_unit"):
                    invoke.append("--work-unit")
                r = call(
                    invoke,
                    a.repo_root.resolve(),
                    env,
                )
                actual = last_json(r.stdout)
                errors, changed = assert_case(
                    actual, expected, before, snap(workspace), workspace, task
                )
                if r.returncode not in expected.get("exit_codes", [0]):
                    errors.append(f"unexpected exit code {r.returncode}")
            except Exception as e:
                errors = [str(e)]
                changed = []
            reports.append(
                {
                    "case": c["case"],
                    "expected": c["expected"],
                    "status": "PASS" if not errors else "FAIL",
                    "changed_paths": changed,
                    "errors": errors,
                }
            )
            print(f"{'PASS' if not errors else 'FAIL'} {c['case']}")
    out = here / "reports"
    out.mkdir(exist_ok=True)
    (out / "results.json").write_text(
        json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (out / "results.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["case", "expected", "status", "errors"])
        w.writeheader()
        [
            w.writerow(
                {
                    "case": x["case"],
                    "expected": x["expected"],
                    "status": x["status"],
                    "errors": " | ".join(x["errors"]),
                }
            )
            for x in reports
        ]
    return 1 if any(x["status"] == "FAIL" for x in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
