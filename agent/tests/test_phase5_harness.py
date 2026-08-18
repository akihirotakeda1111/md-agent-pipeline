from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from agent.errors import AgentError
from agent.spec import parse_spec

REPO_ROOT = Path(__file__).resolve().parents[2]
SUITE = REPO_ROOT / "agent" / "integration-tests" / "github-actions"

if str(SUITE) not in sys.path:
    sys.path.insert(0, str(SUITE))

from integration.expectations import PARSE_JOB, check_run  # noqa: E402
from integration.reporting import (  # noqa: E402
    empty_case_report,
    extract_cycle_result,
    finalize_case_report,
    inspect_codex_api_key_presence,
    redact_codex_api_key,
    strip_gha_log_prefixes,
    write_reports,
)
from integration.runs import (  # noqa: E402
    check_run_identity,
    match_triggered_runs,
    require_unique_run,
)
from integration.spec_template import plan_branches, render_spec  # noqa: E402
from integration.verify_contract import collect_failures  # noqa: E402

FAKE_KEY = "sk-test-not-real-codex-key-value"


def _jobs(parse: str, execute: str) -> list[dict[str, object]]:
    return [
        {"name": PARSE_JOB, "conclusion": parse},
        {"name": "Execute task", "conclusion": execute},
    ]


def test_collect_failures_against_production_yaml() -> None:
    assert collect_failures() == []


def test_render_and_parse_normal_fixture(tmp_path: Path) -> None:
    template = (SUITE / "fixtures" / "01-normal-success.PASS.md").read_text(encoding="utf-8")
    rendered = render_spec(
        template,
        task_id="p5it01deadbeef",
        base_branch="agent/p5it-01-deadbeef",
        target_branch="agent/p5it-01-deadbeef-pr",
    )
    path = tmp_path / "spec.md"
    path.write_text(rendered, encoding="utf-8")
    spec = parse_spec(path)
    assert spec.id == "p5it01deadbeef"
    assert spec.base_branch == "agent/p5it-01-deadbeef"
    assert spec.target_branch == "agent/p5it-01-deadbeef-pr"
    assert spec.base_branch != spec.target_branch
    assert spec.tasks[0].id == "write-result"


def test_render_and_parse_skip_fixture(tmp_path: Path) -> None:
    template = (SUITE / "fixtures" / "03-feature-branch-skip.SKIP.md").read_text(encoding="utf-8")
    rendered = render_spec(
        template,
        task_id="p5it03deadbeef",
        base_branch="main",
        target_branch="agent/p5it-03-deadbeef",
    )
    path = tmp_path / "spec.md"
    path.write_text(rendered, encoding="utf-8")
    spec = parse_spec(path)
    assert spec.id == "p5it03deadbeef"
    assert spec.base_branch == "main"


def test_render_and_parse_dispatch_skip_fixture(tmp_path: Path) -> None:
    template = (SUITE / "fixtures" / "04-dispatch-skip.SKIP.md").read_text(encoding="utf-8")
    rendered = render_spec(
        template,
        task_id="p5it04deadbeef",
        base_branch="main",
        target_branch="agent/p5it-04-deadbeef",
    )
    path = tmp_path / "spec.md"
    path.write_text(rendered, encoding="utf-8")
    spec = parse_spec(path)
    assert spec.id == "p5it04deadbeef"
    assert spec.base_branch == "main"
    assert spec.tasks[0].id == "probe"


def test_invalid_fixture_is_rejected(tmp_path: Path) -> None:
    template = (SUITE / "fixtures" / "02-invalid-spec.INVALID_SPEC.md").read_text(encoding="utf-8")
    rendered = render_spec(
        template,
        task_id="p5it02deadbeef",
        base_branch="main",
        target_branch="unused",
    )
    path = tmp_path / "spec.md"
    path.write_text(rendered, encoding="utf-8")
    with pytest.raises(AgentError) as exc:
        parse_spec(path)
    assert exc.value.category.name in {"INVALID_INPUT", "POLICY_VIOLATION"}


def test_check_run_accepts_skip_conclusions() -> None:
    errors = check_run(
        expected_conclusion="success",
        expected_parse="success",
        expected_execute="skipped",
        expected_event="push",
        conclusion="success",
        jobs=_jobs("success", "skipped"),
        event="push",
    )
    assert errors == []


def test_check_run_rejects_wrong_job_names() -> None:
    errors = check_run(
        expected_conclusion="success",
        expected_parse="success",
        expected_execute="success",
        expected_event="push",
        conclusion="success",
        jobs=[
            {"name": "parse-spec", "conclusion": "success"},
            {"name": "execute", "conclusion": "success"},
        ],
        event="push",
    )
    assert any("Parse spec" in item for item in errors)


def test_cases_json_matches_four_acceptance_ids() -> None:
    config = json.loads((SUITE / "cases.json").read_text(encoding="utf-8"))
    ids = [case["id"] for case in config["cases"]]
    assert ids == [
        "01-normal-success",
        "02-invalid-spec",
        "03-feature-branch-skip",
        "04-dispatch-skip",
    ]
    assert config["workflow"] == "agent-execute.yml"
    assert "dispatch_spec_path" not in config
    assert config["cases"][0]["base_branch_mode"] == "self"
    assert config["cases"][0]["parse_job"] == "success"
    assert config["cases"][0]["execute_job"] == "success"
    assert config["cases"][0]["expected_conclusion"] == "success"
    assert config["cases"][3]["fixture"] == "04-dispatch-skip.SKIP.md"


def test_plan_branches_self_mode_uses_distinct_target() -> None:
    task_id, source, target, base = plan_branches(
        "01-normal-success",
        base_branch_mode="self",
        default_ref="main",
        suffix="deadbeef",
    )
    assert task_id == "p5it01deadbeef"
    assert source == "agent/p5it-01-deadbeef"
    assert base == source
    assert target == "agent/p5it-01-deadbeef-pr"
    assert target != source


def test_plan_branches_default_mode_keeps_single_ephemeral() -> None:
    _, source, target, base = plan_branches(
        "03-feature-branch-skip",
        base_branch_mode="default",
        default_ref="main",
        suffix="deadbeef",
    )
    assert source == "agent/p5it-03-deadbeef"
    assert target == source
    assert base == "main"


SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BRANCH = "agent/p5it-04-deadbeef"


def _run_item(
    run_id: int,
    event: str,
    *,
    branch: str = BRANCH,
    sha: str = SHA,
) -> dict[str, object]:
    return {
        "databaseId": run_id,
        "event": event,
        "headBranch": branch,
        "headSha": sha,
    }


def test_match_triggered_runs_requires_sha() -> None:
    with pytest.raises(ValueError):
        match_triggered_runs([], branch=BRANCH, event="push", head_sha="", known_ids=set())


def test_match_triggered_runs_is_unique_on_branch_sha_event() -> None:
    items = [
        _run_item(1, "push"),
        _run_item(2, "workflow_dispatch"),
        _run_item(3, "workflow_dispatch", sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
        _run_item(4, "workflow_dispatch", branch="other"),
        _run_item(5, "workflow_dispatch"),
    ]
    matches = match_triggered_runs(
        items,
        branch=BRANCH,
        event="workflow_dispatch",
        head_sha=SHA,
        known_ids={5},
    )
    assert require_unique_run(matches, branch=BRANCH, event="workflow_dispatch", head_sha=SHA) == 2


def test_require_unique_run_rejects_ambiguity() -> None:
    matches = [_run_item(2, "push"), _run_item(9, "push")]
    with pytest.raises(RuntimeError, match="ambiguous"):
        require_unique_run(matches, branch=BRANCH, event="push", head_sha=SHA)


def test_check_run_identity_rejects_wrong_sha() -> None:
    errors = check_run_identity(
        expected_event="push",
        expected_branch=BRANCH,
        expected_sha=SHA,
        event="push",
        branch=BRANCH,
        sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    )
    assert any("head_sha" in item for item in errors)


def _assert_no_secret(text: str) -> None:
    assert FAKE_KEY not in text
    assert "sk-test-not-real" not in text


def test_redact_codex_api_key_strips_assignment_and_json_values() -> None:
    raw = "\n".join(
        [
            f"      CODEX_API_KEY: {FAKE_KEY}",
            f"CODEX_API_KEY={FAKE_KEY}",
            json.dumps({"CODEX_API_KEY": FAKE_KEY}),
            "CODEX_API_KEY:",
            "CODEX_API_KEY: ",
            ' "CODEX_API_KEY": "" ',
        ]
    )
    redacted = redact_codex_api_key(raw)
    _assert_no_secret(redacted)
    assert "CODEX_API_KEY: present" in redacted
    assert "CODEX_API_KEY=present" in redacted
    assert '"CODEX_API_KEY": "present"' in redacted
    assert inspect_codex_api_key_presence(raw) is True
    assert inspect_codex_api_key_presence(redacted) is True
    twice = redact_codex_api_key(redacted)
    _assert_no_secret(twice)
    assert redact_codex_api_key("CODEX_API_KEY:") == "CODEX_API_KEY:absent"
    assert "CODEX_API_KEY: absent" in twice


def test_inspect_codex_api_key_presence_absent_and_missing() -> None:
    assert inspect_codex_api_key_presence("no secret here") is None
    assert inspect_codex_api_key_presence("CODEX_API_KEY:") is False
    assert inspect_codex_api_key_presence("CODEX_API_KEY: ***") is True


def test_extract_cycle_result_from_gha_prefixed_pretty_json() -> None:
    cycle = {
        "ok": False,
        "outcome": "ESCALATED",
        "spec_id": "p5it01823eaf55",
        "task_id": "write-result",
        "message": "codex exited non-zero without in-scope changes",
        "classification": "ESCALATION_REQUIRED",
        "repair_attempts": 0,
        "validations": [],
        "scope": {"allowed": True, "changed_paths": [], "violation_paths": [], "reason": None},
    }
    pretty = json.dumps(cycle, indent=2)
    prefixed = "\n".join(
        f"Execute task\tRun orchestrator\t2026-08-17T06:00:26.1582378Z {line}"
        for line in pretty.splitlines()
    )
    stripped = strip_gha_log_prefixes(prefixed)
    assert stripped.splitlines()[0].startswith("{")
    extracted = extract_cycle_result(prefixed)
    assert extracted is not None
    assert extracted["outcome"] == "ESCALATED"
    assert extracted["spec_id"] == "p5it01823eaf55"
    assert extracted["scope"]["changed_paths"] == []


def test_extract_cycle_result_uses_last_matching_object() -> None:
    cycle = {
        "outcome": "ESCALATED",
        "spec_id": "p5it01deadbeef",
        "task_id": "write-result",
        "message": "codex exited non-zero without in-scope changes",
        "repair_attempts": 0,
        "validations": [],
        "CODEX_API_KEY": FAKE_KEY,
    }
    log = "\n".join(
        [
            '{"outcome": "TASK_COMPLETED", "spec_id": "old", "message": "done"}',
            f"      CODEX_API_KEY: {FAKE_KEY}",
            json.dumps(cycle),
        ]
    )
    extracted = extract_cycle_result(log)
    assert extracted is not None
    assert extracted["outcome"] == "ESCALATED"
    assert extracted["spec_id"] == "p5it01deadbeef"
    assert extracted["CODEX_API_KEY"] is True
    _assert_no_secret(json.dumps(extracted))


def test_finalize_and_write_reports_keep_identity_and_redact_logs(tmp_path: Path) -> None:
    case = {
        "id": "01-normal-success",
        "expected_conclusion": "success",
        "trigger": "push",
        "event": "push",
    }
    report = empty_case_report(case)
    report.update(
        {
            "errors": ["workflow: expected success, got failure"],
            "run_id": 123,
            "url": "https://example.test/run/123",
            "head_sha": SHA,
            "branch": BRANCH,
            "conclusion": "failure",
            "jobs": {"Parse spec": "success", "Execute task": "failure"},
            "failed_log": f"CODEX_API_KEY: {FAKE_KEY}\n"
            + json.dumps(
                {
                    "outcome": "ESCALATED",
                    "spec_id": "p5it01deadbeef",
                    "message": "codex exited non-zero without in-scope changes",
                }
            ),
        }
    )
    finalized = finalize_case_report(report)
    assert finalized["status"] == "FAIL"
    assert finalized["codex_api_key_present"] is True
    assert finalized["cycle_result"]["outcome"] == "ESCALATED"
    _assert_no_secret(finalized["failed_log"])
    passing = empty_case_report(case)
    passing["errors"] = []
    passing["run_id"] = 456
    passing = finalize_case_report(passing)
    assert passing["status"] == "PASS"
    write_reports([finalized, passing], tmp_path)
    results = (tmp_path / "results.json").read_text(encoding="utf-8")
    csv_text = (tmp_path / "results.csv").read_text(encoding="utf-8-sig")
    log_text = (tmp_path / "01-normal-success.log").read_text(encoding="utf-8")
    _assert_no_secret(results)
    _assert_no_secret(csv_text)
    _assert_no_secret(log_text)
    payload = json.loads(results)
    assert "failed_log" not in payload["cases"][0]
    assert payload["cases"][0]["failed_log_path"] == "01-normal-success.log"
    assert payload["cases"][1]["failed_log_path"] is None
    assert payload["summary"][0]["status"] == "FAIL"
    assert payload["summary"][1]["status"] == "PASS"
    assert payload["cases"][0]["url"] == "https://example.test/run/123"
    assert payload["cases"][0]["run_id"] == 123
    assert "01-normal-success" in csv_text
    assert "https://example.test/run/123" in csv_text
    assert "CODEX_API_KEY: present" in log_text
