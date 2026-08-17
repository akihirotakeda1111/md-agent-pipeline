"""Local report helpers for Phase 5 GitHub Actions integration tests."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

SECRET_NAME = "CODEX_API_KEY"
PRESENT = "present"
ABSENT = "absent"

_ABSENT_TOKENS = {"", "null", "none", "undefined", "absent", "false"}

_ASSIGN = re.compile(
    rf"(?P<prefix>{re.escape(SECRET_NAME)}[ \t]*[=:][ \t]*)(?P<value>[^\s\"']+)",
    re.IGNORECASE,
)
_JSON_DOUBLE = re.compile(
    rf'(?P<prefix>"{re.escape(SECRET_NAME)}"\s*:\s*")(?P<value>[^"]*)(?P<suffix>")',
    re.IGNORECASE,
)
_JSON_SINGLE = re.compile(
    rf"(?P<prefix>'{re.escape(SECRET_NAME)}'\s*:\s*')(?P<value>[^']*)(?P<suffix>')",
    re.IGNORECASE,
)
_ENV_LINE = re.compile(
    rf"^(?P<prefix>[ \t]*{re.escape(SECRET_NAME)}[ \t]*:[ \t]*)(?P<value>.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def _is_present_token(value: str) -> bool:
    stripped = value.strip().strip("\"'")
    return stripped.lower() not in _ABSENT_TOKENS


def inspect_codex_api_key_presence(text: str) -> bool | None:
    """Return whether a CODEX_API_KEY value appears, without exposing it."""
    if SECRET_NAME not in text:
        return None
    seen = False
    present = False
    for pattern in (_ENV_LINE, _ASSIGN, _JSON_DOUBLE, _JSON_SINGLE):
        for match in pattern.finditer(text):
            seen = True
            if _is_present_token(match.group("value")):
                present = True
    if not seen:
        return None
    return present


def redact_codex_api_key(text: str) -> str:
    """Replace any CODEX_API_KEY value with present/absent. Never keep the value."""

    def replace_assign(match: re.Match[str]) -> str:
        token = PRESENT if _is_present_token(match.group("value")) else ABSENT
        return f"{match.group('prefix')}{token}"

    def replace_json(match: re.Match[str]) -> str:
        token = PRESENT if _is_present_token(match.group("value")) else ABSENT
        return f"{match.group('prefix')}{token}{match.group('suffix')}"

    redacted = _JSON_DOUBLE.sub(replace_json, text)
    redacted = _JSON_SINGLE.sub(replace_json, redacted)
    redacted = _ENV_LINE.sub(replace_assign, redacted)
    redacted = _ASSIGN.sub(replace_assign, redacted)
    return redacted


def scrub_payload(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, inner in value.items():
            if key == SECRET_NAME:
                cleaned[key] = bool(inner) if not isinstance(inner, bool) else inner
            else:
                cleaned[key] = scrub_payload(inner)
        return cleaned
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    if isinstance(value, str):
        return redact_codex_api_key(value)
    return value


_GHA_TIMESTAMP = re.compile(r"(?:\ufeff)?\d{4}-\d{2}-\d{2}T[\d:.]+Z[ ]?")


def strip_gha_log_prefixes(text: str) -> str:
    """Remove GitHub Actions job/step/timestamp prefixes so JSON can be parsed."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.lstrip("\ufeff")
        match = _GHA_TIMESTAMP.search(line)
        if match:
            line = line[match.end() :]
        else:
            parts = line.split("\t")
            if len(parts) >= 3:
                line = parts[-1]
        lines.append(line)
    return "\n".join(lines)


def extract_cycle_result(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    for candidate in (strip_gha_log_prefixes(text), text):
        index = 0
        while index < len(candidate):
            brace = candidate.find("{", index)
            if brace < 0:
                break
            try:
                parsed, end = decoder.raw_decode(candidate, brace)
            except json.JSONDecodeError:
                index = brace + 1
                continue
            if (
                isinstance(parsed, dict)
                and "outcome" in parsed
                and "spec_id" in parsed
                and "message" in parsed
            ):
                found.append(parsed)
            index = end
        if found:
            break
    if not found:
        return None
    return scrub_payload(found[-1])


def empty_case_report(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case": case["id"],
        "expected": case["expected_conclusion"],
        "status": "FAIL",
        "errors": [],
        "trigger": case.get("trigger"),
        "event": case.get("event"),
        "spec_path": None,
        "task_id": None,
        "run_id": None,
        "url": None,
        "head_sha": None,
        "branch": None,
        "conclusion": None,
        "jobs": {},
        "codex_api_key_present": None,
        "cycle_result": None,
        "failed_log": None,
        "failed_log_path": None,
    }


def finalize_case_report(report: dict[str, Any]) -> dict[str, Any]:
    errors = [redact_codex_api_key(str(item)) for item in report.get("errors") or []]
    report["errors"] = errors
    report["status"] = "PASS" if not errors else "FAIL"
    if isinstance(report.get("failed_log"), str):
        raw_log = report["failed_log"]
        if report.get("codex_api_key_present") is None:
            report["codex_api_key_present"] = inspect_codex_api_key_presence(raw_log)
        report["failed_log"] = redact_codex_api_key(raw_log)
        if report.get("cycle_result") is None:
            report["cycle_result"] = extract_cycle_result(report["failed_log"])
    report["cycle_result"] = scrub_payload(report.get("cycle_result"))
    return report


def write_reports(rows: list[dict[str, Any]], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    exported: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        log_text = item.pop("failed_log", None)
        if isinstance(log_text, str) and log_text.strip():
            filename = f"{item['case']}.log"
            (report_dir / filename).write_text(log_text, encoding="utf-8")
            item["failed_log_path"] = filename
        else:
            item["failed_log_path"] = item.get("failed_log_path")
        exported.append(item)
    summary = [
        {
            "case": row["case"],
            "expected": row.get("expected"),
            "status": row.get("status"),
            "matched": row.get("status") == "PASS",
        }
        for row in exported
    ]
    payload = scrub_payload({"summary": summary, "cases": exported})
    (report_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fieldnames = [
        "case",
        "expected",
        "status",
        "errors",
        "trigger",
        "event",
        "spec_path",
        "task_id",
        "run_id",
        "url",
        "head_sha",
        "branch",
        "conclusion",
        "jobs",
        "codex_api_key_present",
        "failed_log_path",
    ]
    with (report_dir / "results.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in exported:
            jobs = row.get("jobs") or {}
            writer.writerow(
                {
                    "case": row.get("case"),
                    "expected": row.get("expected"),
                    "status": row.get("status"),
                    "errors": " | ".join(row.get("errors") or []),
                    "trigger": row.get("trigger") or "",
                    "event": row.get("event") or "",
                    "spec_path": row.get("spec_path") or "",
                    "task_id": row.get("task_id") or "",
                    "run_id": row.get("run_id") or "",
                    "url": row.get("url") or "",
                    "head_sha": row.get("head_sha") or "",
                    "branch": row.get("branch") or "",
                    "conclusion": row.get("conclusion") or "",
                    "jobs": json.dumps(jobs, ensure_ascii=False) if jobs else "",
                    "codex_api_key_present": (
                        ""
                        if row.get("codex_api_key_present") is None
                        else str(row.get("codex_api_key_present"))
                    ),
                    "failed_log_path": row.get("failed_log_path") or "",
                }
            )
