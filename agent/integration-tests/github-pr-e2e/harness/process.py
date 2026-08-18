from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], completed: subprocess.CompletedProcess[str]) -> None:
        safe = " ".join(argv)
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        super().__init__(f"command failed ({completed.returncode}): {safe}\n{detail}")


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    effective = dict(os.environ)
    if env:
        effective.update(env)
    completed = subprocess.run(
        argv, cwd=cwd, env=effective, text=True, input=input_text, capture_output=True, check=False
    )
    if check and completed.returncode != 0:
        raise CommandError(argv, completed)
    return completed


def json_output(argv: list[str], *, cwd: Path | None = None) -> Any:
    completed = run(argv, cwd=cwd)
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {' '.join(argv)}") from exc
