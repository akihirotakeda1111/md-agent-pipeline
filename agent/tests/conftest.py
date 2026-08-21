from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def current_python_is_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the interpreter running pytest available to validation subprocesses."""
    python_dir = str(Path(sys.executable).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if python_dir not in path_parts:
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join((python_dir, *filter(None, path_parts))),
        )
