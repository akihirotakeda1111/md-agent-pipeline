from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "agent" / "scripts"
EXAMPLE_SPEC = REPO_ROOT / "specs" / "tasks" / "example-task.md"


def _run(script: str, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        cwd=str(cwd or REPO_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )


def test_parse_spec_script_emits_json() -> None:
    result = _run("parse-spec.py", str(EXAMPLE_SPEC))
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["spec"]["id"] == "phase2-step2"


def test_validate_spec_script_rejects_invalid_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "bad.md"
    spec_path.write_text("---\nid: [\n---\n# Objective\n\nx\n", encoding="utf-8")
    result = _run("validate-spec.py", str(spec_path))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["code"] == "INVALID_SPEC"


def test_init_update_and_select_scripts(tmp_path: Path) -> None:
    init = _run("init-state.py", "--spec", str(EXAMPLE_SPEC), "--repo-root", str(tmp_path))
    assert init.returncode == 0, init.stdout
    created = json.loads(init.stdout)
    assert created["state"]["state"] == "PENDING"

    updated = _run(
        "update-state.py",
        "--task-id",
        "phase2-step2",
        "--to",
        "RUNNING",
        "--repo-root",
        str(tmp_path),
        "--set-json",
        json.dumps({"currentTask": "task-1"}),
    )
    assert updated.returncode == 0, updated.stdout
    payload = json.loads(updated.stdout)
    assert payload["state"]["state"] == "RUNNING"
    assert payload["state"]["currentTask"] == "task-1"

    selected = _run(
        "select-task.py",
        "--spec",
        str(EXAMPLE_SPEC),
        "--repo-root",
        str(tmp_path),
    )
    assert selected.returncode == 0, selected.stdout
    choice = json.loads(selected.stdout)
    assert choice["task_id"] == "task-1"


def test_update_state_script_rejects_invalid_transition(tmp_path: Path) -> None:
    assert (
        _run("init-state.py", "--spec", str(EXAMPLE_SPEC), "--repo-root", str(tmp_path)).returncode
        == 0
    )
    result = _run(
        "update-state.py",
        "--task-id",
        "phase2-step2",
        "--to",
        "COMPLETED",
        "--repo-root",
        str(tmp_path),
    )
    assert result.returncode == 3
    payload = json.loads(result.stdout)
    assert payload["code"] == "INVALID_TRANSITION"
