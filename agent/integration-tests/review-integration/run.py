from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_BINDING = "integration.invoke_phase7"


def load_cases() -> list[dict[str, str]]:
    return json.loads((ROOT / "cases.json").read_text(encoding="utf-8"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Phase 7 Production integration suite")
    parser.add_argument("--production-root", type=Path, default=Path.cwd())
    parser.add_argument("--binding", default=os.environ.get("PHASE7_BINDING", DEFAULT_BINDING))
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "reports")
    parser.add_argument("--pytest-arg", action="append", default=[])
    return parser.parse_args()


def preflight(production_root: Path, binding: str) -> list[str]:
    errors: list[str] = []
    if importlib.util.find_spec("pytest") is None:
        errors.append("pytest is not installed")
    if importlib.util.find_spec("yaml") is None:
        errors.append("PyYAML is not installed")
    if shutil.which("git") is None:
        errors.append("git executable was not found on PATH")
    workflow = production_root / ".github" / "workflows" / "agent-review.yml"
    if not workflow.is_file():
        errors.append(f"Production workflow not found: {workflow}")
    for path in (str(ROOT), str(production_root)):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        module = importlib.import_module(binding)
        module.create_driver()
    except Exception as exc:
        errors.append(f"Production binding unavailable ({binding}): {exc}")
    return errors


def selected_nodes(case_ids: list[str] | None, cases: list[dict[str, str]]) -> list[str]:
    if not case_ids:
        return [str(ROOT / "integration")]
    by_id = {case["id"].upper(): case for case in cases}
    unknown = [case_id for case_id in case_ids if case_id.upper() not in by_id]
    if unknown:
        raise ValueError(f"Unknown case ID(s): {', '.join(unknown)}")
    nodes: list[str] = []
    for case_id in case_ids:
        node = by_id[case_id.upper()]["test"]
        file_name, separator, selector = node.partition("::")
        absolute = str(ROOT / "integration" / file_name)
        nodes.append(f"{absolute}::{selector}" if separator else absolute)
    return list(dict.fromkeys(nodes))


def junit_counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {
        key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }


def write_report(report_dir: Path, payload: dict[str, Any]) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "phase7-result.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    args = parse_args()
    cases = load_cases()
    if args.list:
        for case in cases:
            print(f"{case['id']:>3}  {case['area']:<16} {case['title']}")
        return 0
    started = datetime.now(UTC).isoformat()
    production_root = args.production_root.resolve()
    report_dir = args.report_dir.resolve()
    try:
        nodes = selected_nodes(args.case_ids, cases)
    except ValueError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2
    errors = preflight(production_root, args.binding)
    if errors:
        for error in errors:
            print(f"[PREFLIGHT FAIL] {error}", file=sys.stderr)
        write_report(
            report_dir,
            {
                "status": "PREFLIGHT_FAILED",
                "started_at": started,
                "production_root": str(production_root),
                "binding": args.binding,
                "case_ids": args.case_ids or "ALL",
                "errors": errors,
            },
        )
        return 2
    report_dir.mkdir(parents=True, exist_ok=True)
    junit = report_dir / "pytest.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(ROOT / "pytest.ini"),
        *nodes,
        "--phase7-binding",
        args.binding,
        "--phase7-production-root",
        str(production_root),
        "--require-phase7-binding",
        "--junitxml",
        str(junit),
        *args.pytest_arg,
    ]
    env = dict(os.environ)
    env["PHASE7_BINDING"] = args.binding
    env["PHASE7_PRODUCTION_ROOT"] = str(production_root)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), str(production_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    completed = subprocess.run(command, cwd=production_root, env=env, check=False)
    counts = (
        junit_counts(junit)
        if junit.is_file()
        else {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    )
    passed = completed.returncode == 0 and counts["skipped"] == 0 and counts["tests"] > 0
    status = "PASS" if passed else "FAIL"
    write_report(
        report_dir,
        {
            "status": status,
            "started_at": started,
            "completed_at": datetime.now(UTC).isoformat(),
            "production_root": str(production_root),
            "binding": args.binding,
            "case_ids": args.case_ids or "ALL",
            "pytest_exit_code": completed.returncode,
            "counts": counts,
            "junit": str(junit),
        },
    )
    print(
        f"[Phase 7] {status}: tests={counts['tests']} "
        f"failures={counts['failures']} errors={counts['errors']} "
        f"skipped={counts['skipped']}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
