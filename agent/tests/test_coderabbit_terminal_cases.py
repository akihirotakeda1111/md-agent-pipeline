"""Production mapper tests for observed CodeRabbit terminal payloads.

Expected kinds live in fixtures/coderabbit_terminal_cases.json and are shared
with the E2E harness mapper tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent.config import load_config
from agent.review_terminal import resolve_coderabbit_terminal

CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "coderabbit_terminal_cases.json"
HEAD_CURRENT = "abc"
HEAD_OLD = "old"


def load_observed_payload() -> dict[str, Any]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def bind_observed_case(
    case: dict[str, Any], *, current: str = HEAD_CURRENT, old: str = HEAD_OLD
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    mapping = {"current": current, "old": old}

    def bind_item(item: dict[str, Any], sha_key: str) -> dict[str, Any]:
        bound = dict(item)
        token = bound.get(sha_key)
        if token in mapping:
            bound[sha_key] = mapping[token]
        return bound

    checks = [bind_item(item, "head_sha") for item in case.get("check_runs") or []]
    statuses = [bind_item(item, "sha") for item in case.get("statuses") or []]
    return checks, statuses, current


def _cases() -> list[dict[str, Any]]:
    return list(load_observed_payload()["cases"])


def test_observed_fixture_identity_matches_config() -> None:
    identity = load_observed_payload()["identity"]
    cfg = load_config().coderabbit
    assert identity["actor"] == cfg.actor
    assert identity["check_app_slug"] == cfg.check_app_slug
    assert identity["status_context"] == cfg.status_context


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
def test_production_mapper_observed_case(case: dict[str, Any]) -> None:
    cfg = load_config().coderabbit
    checks, statuses, head = bind_observed_case(case)
    first = resolve_coderabbit_terminal(checks, statuses, head_sha=head, cfg=cfg)
    second = resolve_coderabbit_terminal(checks, statuses, head_sha=head, cfg=cfg)
    expected = case["expected"]
    assert first.kind.value == expected["kind"]
    assert first.source == expected.get("source", first.source)
    assert first.description == expected.get("description", first.description)
    assert first == second
    if expected["kind"] == "CODERABBIT_SKIPPED":
        assert first.is_escalating()
        assert first.escalation_code() == "CODERABBIT_SKIPPED"
