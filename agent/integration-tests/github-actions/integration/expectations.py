"""Assertions for production Agent Execute job names and conclusions."""

from __future__ import annotations

PARSE_JOB = "Parse spec"
EXECUTE_JOB = "Execute task"
DELIVER_JOB = "Deliver commit and pull request"


def job_conclusions(jobs: list[dict[str, object]]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for job in jobs:
        name = job.get("name")
        if isinstance(name, str):
            conclusion = job.get("conclusion")
            result[name] = conclusion if isinstance(conclusion, str) else None
    return result


def check_run(
    *,
    expected_conclusion: str,
    expected_parse: str,
    expected_execute: str,
    expected_event: str,
    conclusion: str | None,
    jobs: list[dict[str, object]],
    event: str | None,
    expected_deliver: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if conclusion != expected_conclusion:
        errors.append(f"workflow: expected {expected_conclusion}, got {conclusion}")
    by_name = job_conclusions(jobs)
    if by_name.get(PARSE_JOB) != expected_parse:
        errors.append(f"{PARSE_JOB}: expected {expected_parse}, got {by_name.get(PARSE_JOB)}")
    if by_name.get(EXECUTE_JOB) != expected_execute:
        errors.append(f"{EXECUTE_JOB}: expected {expected_execute}, got {by_name.get(EXECUTE_JOB)}")
    if expected_deliver is not None and by_name.get(DELIVER_JOB) != expected_deliver:
        errors.append(f"{DELIVER_JOB}: expected {expected_deliver}, got {by_name.get(DELIVER_JOB)}")
    if event != expected_event:
        errors.append(f"event: expected {expected_event}, got {event}")
    return errors
