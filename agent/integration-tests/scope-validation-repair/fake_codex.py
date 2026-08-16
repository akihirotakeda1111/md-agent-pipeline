#!/usr/bin/env python3
"""Deterministic Phase 3 executable double used through the real Codex Runner."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

prompt = sys.stdin.read()
root = Path.cwd()


def write(path: str, text: str):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


stage = "repair" if "you are repairing" in prompt.lower() else "initial"
if "CASE_01_NORMAL" in prompt:
    write("app/result.txt", "valid\n")
elif "CASE_02_SCOPE" in prompt:
    write("specs/tasks/owned.md", "illegal\n")
elif "CASE_09_STATE_SCOPE" in prompt:
    write(".agent/state/leaked.json", "{}\n")
elif "CASE_03_REPAIR_SUCCESS" in prompt:
    write("app/result.txt", "valid\n" if stage == "repair" else "invalid\n")
elif "CASE_04_REPAIR_SCOPE" in prompt:
    if stage == "repair":
        write("agent/config.json", "{}\n")
    else:
        write("app/result.txt", "invalid\n")
elif "CASE_05_LIMIT" in prompt:
    write("app/result.txt", "invalid\n")
elif "CASE_06_ENVIRONMENT" in prompt:
    write("app/result.txt", "candidate\n")
elif "CASE_07_ESCALATION" in prompt:
    write("app/result.txt", "invalid\n")
    if stage == "repair":
        print(
            json.dumps({"type": "IMPLEMENTATION_BLOCKED", "reason": "constraints prevent repair"})
        )
elif "CASE_08_FINAL" in prompt:
    write("app/result.txt", "valid\n")
print(
    json.dumps(
        {
            "type": "phase4.fake_codex.capture",
            "stage": stage,
            "cwd": str(root),
            "argv": sys.argv[1:],
            "env_keys": sorted(k for k in os.environ if k.startswith("CODEX_")),
        }
    )
)
