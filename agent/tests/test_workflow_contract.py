from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = (
    REPO_ROOT
    / "agent"
    / "integration-tests"
    / "github-actions"
    / "integration"
    / "verify_contract.py"
)


def test_production_workflow_contract() -> None:
    completed = subprocess.run(
        [sys.executable, str(CONTRACT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "PASS" in completed.stdout
