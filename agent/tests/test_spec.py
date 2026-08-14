from __future__ import annotations

from pathlib import Path

import pytest
from agent.errors import AgentError
from agent.spec import parse_spec, parse_spec_text, spec_to_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_SPEC = REPO_ROOT / "specs" / "tasks" / "example-task.md"
TEMPLATE_SPEC = REPO_ROOT / "specs" / "tasks" / "TEMPLATE.md"

VALID_BODY = """
# Objective

Ship the worker.

# Non-Goals

Do not add a framework.

# Forbidden Actions

Do not edit specs.

# Architecture Invariants

State stays outside the spec.

# Tasks

## task-1: Lease repository

### Requirement

Store leases.

### Acceptance Criteria

- Acquire is idempotent.

### Validation

pytest worker/tests/test_lease_repository.py

## task-2: Heartbeat loop

depends_on: task-1

### Requirement

Refresh owned leases.

### Acceptance Criteria

- Expired leases fail closed.

### Validation

pytest worker/tests/test_heartbeat_loop.py

# Final Verification

pytest worker/tests
"""

VALID_META = {
    "schema_version": 1,
    "id": "phase2-step2",
    "title": "Idempotent Worker",
    "status": "PENDING",
    "base_branch": "main",
    "target_branch": "feature/phase2-worker",
    "allowed_paths": ["worker/**"],
    "forbidden_paths": ["specs/**"],
    "repair_attempt_limit": 3,
    "review_attempt_limit": 3,
}


def _frontmatter(overrides: dict | None = None, omit: set[str] | None = None) -> str:
    data = dict(VALID_META)
    if overrides:
        data.update(overrides)
    for key in omit or set():
        data.pop(key, None)
    lines = ["---"]
    for key, value in data.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _spec_text(
    *,
    overrides: dict | None = None,
    omit: set[str] | None = None,
    body: str = VALID_BODY,
) -> str:
    return _frontmatter(overrides, omit) + body


def test_example_task_parses() -> None:
    spec = parse_spec(EXAMPLE_SPEC)
    payload = spec_to_dict(spec)

    assert payload["id"] == "phase2-step2"
    assert payload["schema_version"] == 1
    assert payload["allowed_paths"] == ["worker/**", "infra/terraform/**"]
    assert [task["id"] for task in payload["tasks"]] == ["task-1", "task-2"]
    assert payload["tasks"][1]["depends_on"] == ["task-1"]
    assert "Objective" not in payload
    assert "idempotent worker" in payload["objective"].lower()
    assert "pytest worker/tests" in payload["final_verification"]


def test_template_task_parses() -> None:
    spec = parse_spec(TEMPLATE_SPEC)
    assert spec.id == "your-task-id"
    assert len(spec.tasks) == 1
    assert spec.tasks[0].id == "task-1"


def test_invalid_yaml_is_invalid_spec() -> None:
    text = "---\nid: [\n---\n" + VALID_BODY
    with pytest.raises(AgentError) as exc_info:
        parse_spec_text(text)
    assert exc_info.value.code == "INVALID_SPEC"


def test_missing_frontmatter_is_invalid_spec() -> None:
    with pytest.raises(AgentError) as exc_info:
        parse_spec_text(VALID_BODY)
    assert exc_info.value.code == "INVALID_SPEC"
    assert "frontmatter" in str(exc_info.value)


def test_missing_required_property_is_invalid_spec() -> None:
    with pytest.raises(AgentError) as exc_info:
        parse_spec_text(_spec_text(omit={"title"}))
    assert exc_info.value.code == "INVALID_SPEC"
    assert "title" in str(exc_info.value)


def test_unsupported_schema_version_is_invalid_spec() -> None:
    with pytest.raises(AgentError) as exc_info:
        parse_spec_text(_spec_text(overrides={"schema_version": 99}))
    assert exc_info.value.code == "INVALID_SPEC"
    assert "unsupported schema_version" in str(exc_info.value)


def test_zero_tasks_is_invalid_spec() -> None:
    body = """
# Objective

Ship the worker.

# Non-Goals

None.

# Forbidden Actions

None.

# Architecture Invariants

None.

# Tasks

# Final Verification

pytest
"""
    with pytest.raises(AgentError) as exc_info:
        parse_spec_text(_spec_text(body=body))
    assert exc_info.value.code == "INVALID_SPEC"
    assert "at least one task" in str(exc_info.value).lower() or "Tasks" in str(exc_info.value)


def test_task_missing_acceptance_criteria_is_invalid_spec() -> None:
    body = """
# Objective

Ship the worker.

# Non-Goals

None.

# Forbidden Actions

None.

# Architecture Invariants

None.

# Tasks

## task-1: Lease repository

### Requirement

Store leases.

### Validation

pytest

# Final Verification

pytest
"""
    with pytest.raises(AgentError) as exc_info:
        parse_spec_text(_spec_text(body=body))
    assert exc_info.value.code == "INVALID_SPEC"
    assert "Acceptance Criteria" in str(exc_info.value)
