#!/usr/bin/env python3
"""Deterministic executable double for the official Codex CLI."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

FAKE_VERSION = "codex-cli 0.147.0-phase3-fake"


def _write(path: Path, content: str, root: Path, changed: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8", newline="\n")
    changed.append(path.relative_to(root).as_posix())


def _last_message_path(argv: list[str]) -> Path:
    try:
        return Path(argv[argv.index("--output-last-message") + 1])
    except (ValueError, IndexError) as exc:
        raise ValueError("--output-last-message is required") from exc


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print(FAKE_VERSION)
        return 0
    if not argv or argv[0] != "exec":
        print("fake Codex requires the exec subcommand", file=sys.stderr)
        return 64
    try:
        sandbox_index = argv.index("--sandbox")
        if argv[sandbox_index + 1] != "workspace-write":
            raise ValueError("sandbox must be workspace-write")
        final_path = _last_message_path(argv)
    except (ValueError, IndexError) as exc:
        print(str(exc), file=sys.stderr)
        return 64
    if "--json" not in argv or argv[-1] != "-":
        print("fake Codex requires --json and stdin prompt", file=sys.stderr)
        return 64

    prompt = sys.stdin.read()
    root = Path.cwd().resolve()
    changed: list[str] = []
    inspected: list[str] = []
    status = "PASS"

    if "CASE_01_CREATE_FILE" in prompt:
        _write(root / "app/greeting.txt", "Hello from Codex\n", root, changed)
    elif "CASE_02_MODIFY_FILE" in prompt:
        path = root / "app/message.txt"
        inspected.append("app/message.txt")
        original = path.read_text(encoding="utf-8")
        if original.count("status=old") != 1:
            raise RuntimeError("case 02 baseline must contain exactly one status=old")
        _write(path, original.replace("status=old", "status=new"), root, changed)
    elif "CASE_03_INSPECT_THEN_MODIFY" in prompt:
        config = root / "config.txt"
        inspected.append("config.txt")
        match = re.fullmatch(r"prefix=([^\r\n]+)\r?\n?", config.read_text(encoding="utf-8"))
        if match is None:
            raise RuntimeError("case 03 config format is invalid")
        _write(root / "app/banner.txt", f"{match.group(1)}-ready\n", root, changed)
    elif "CASE_04_ADD_FUNCTION" in prompt:
        path = root / "src/math_utils.py"
        inspected.append("src/math_utils.py")
        original = path.read_text(encoding="utf-8")
        if "def increment(value):" not in original:
            raise RuntimeError("case 04 did not inspect the existing module")
        content = original.rstrip() + "\n\n\ndef double(value):\n    return value * 2\n"
        _write(path, content, root, changed)
    elif "CASE_05_MULTIPLE_FILES" in prompt:
        feature = root / "app/feature.txt"
        inspected.append("app/feature.txt")
        if feature.read_text(encoding="utf-8") != "disabled\n":
            raise RuntimeError("case 05 baseline is invalid")
        _write(feature, "enabled\n", root, changed)
        _write(root / "docs/feature.txt", "Feature is enabled\n", root, changed)
    elif "CASE_06_NO_CHANGE" in prompt:
        greeting = root / "app/greeting.txt"
        inspected.append("app/greeting.txt")
        if greeting.read_text(encoding="utf-8") != "Hello from Codex\n":
            raise RuntimeError("case 06 is not already satisfied")
    elif "CASE_07_PROTECTED_PATH_BLOCKED" in prompt:
        inspected.extend(["app/existing.txt", "specs/tasks/registry.md"])
        if not (root / "specs/tasks/registry.md").is_file():
            raise RuntimeError("case 07 protected registry is missing")
        status = "BLOCKED"
    else:
        print("Current Task marker was not present in the prompt", file=sys.stderr)
        return 64

    if status == "BLOCKED":
        final_response = (
            "IMPLEMENTATION_BLOCKED\n\n"
            "Reason: the selected task requires a protected-path change\n"
            "Required change: revise the task scope or provide an application-only task\n"
            "Conflicting constraint: specs/tasks/** is forbidden and outside allowed_paths\n"
        )
    elif changed:
        final_response = "CODEX_EXECUTED\nWORKSPACE_CHANGED\n"
    else:
        final_response = "CODEX_EXECUTED\nNO_CHANGE\n"

    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_text(final_response, encoding="utf-8", newline="\n")
    event = {
        "type": "phase3.fake_codex.capture",
        "argv": argv,
        "prompt": prompt,
        "working_directory": str(root),
        "environment_keys": sorted(os.environ),
        "inspected_files": inspected,
        "changed_files": changed,
        "reported_status": status,
    }
    print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
