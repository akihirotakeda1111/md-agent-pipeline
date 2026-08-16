#!/usr/bin/env python3
"""Test-only adapter that injects a fake executable into the real Phase 3 Runner."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True)
    args = parser.parse_args()

    source_repo = args.source_repo.resolve()
    if str(source_repo) not in sys.path:
        sys.path.insert(0, str(source_repo))

    from agent.codex_runner import resolve_task, run_codex
    from agent.config import load_config
    from agent.spec import parse_spec

    spec = parse_spec(args.spec)
    task = resolve_task(spec, args.task)
    config = load_config()
    test_config = replace(config, codex=replace(config.codex, bin=args.codex_bin))
    result = run_codex(spec, task, repo_root=args.repo_root, config=test_config)
    print(json.dumps({"ok": result.exit_code == 0, **result.to_json_dict()}, ensure_ascii=False))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
