from __future__ import annotations

import argparse
import importlib.util
import json
import re
import secrets
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.assertions import (
    assert_commit_files,
    assert_no_new_delivery_events,
    assert_pr,
    assert_pr_files,
    assert_restart_run,
    assert_reuse,
    assert_run_identity_count,
    assert_source_tip_matches_run,
    assert_successful_run,
)
from harness.git import GitRepository
from harness.github import GitHub
from harness.models import ProductionGap, Scenario, report_skeleton
from harness.source_contracts import inspect_source_contracts

SUITE = Path(__file__).resolve().parent
WORKFLOW_FILE = "agent-execute.yml"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,39}$")
SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Real GitHub Phase 5-6 E2E smoke scenario")
    parser.add_argument(
        "--repo", required=True, help="OWNER/REPO containing the Production workflow"
    )
    parser.add_argument(
        "--base-branch",
        help=(
            "Repository branch to fork the temporary E2E base from; "
            "defaults to the repository default branch"
        ),
    )
    parser.add_argument("--unique-id", help="Safe unique suffix; generated when omitted")
    parser.add_argument("--trigger", choices=("auto", "push", "workflow_dispatch"), default="push")
    parser.add_argument(
        "--dispatch-input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Existing Production workflow_dispatch input; repeatable",
    )
    parser.add_argument(
        "--keep-resources", action="store_true", help="Leave PR and branches for investigation"
    )
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--discovery-timeout-seconds", type=int, default=180)
    parser.add_argument("--reports-dir", type=Path, default=SUITE / "reports")
    return parser.parse_args()


def unique_id(value: str | None) -> str:
    generated = value or f"{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
    if not SAFE_ID.fullmatch(generated):
        raise ValueError("--unique-id must match [a-z0-9][a-z0-9-]{5,39}")
    return generated


def dispatch_inputs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key:
            raise ValueError(f"invalid --dispatch-input: {value}")
        result[key] = item
    return result


def make_scenario(repo: str, suffix: str) -> Scenario:
    source_branch = f"e2e/phase6-{suffix}"
    scenario = Scenario(
        scenario_id=f"github-pr-e2e-{suffix}",
        repo=repo,
        base_branch=source_branch,
        source_branch=source_branch,
        task_spec=f"specs/tasks/_e2e-phase6-{suffix}.md",
        task_id=f"phase6-e2e-{suffix}",
        target_branch=f"agent/phase6-e2e-{suffix}",
        generated_file=f"app/e2e-phase6-{suffix}.txt",
        generated_content=f"phase6-e2e-{suffix}",
    )
    assert scenario.source_branch.startswith("e2e/phase6-")
    assert scenario.target_branch.startswith("agent/phase6-e2e-")
    assert scenario.base_branch == scenario.source_branch
    return scenario


def render_spec(scenario: Scenario) -> str:
    template = (SUITE / "fixtures" / "phase6-e2e-task.md").read_text(encoding="utf-8")
    replacements = {
        "{{UNIQUE_ID}}": scenario.scenario_id.removeprefix("github-pr-e2e-"),
        "{{TASK_ID}}": scenario.task_id,
        "{{BASE_BRANCH}}": scenario.base_branch,
        "{{TARGET_BRANCH}}": scenario.target_branch,
        "{{GENERATED_FILE}}": scenario.generated_file,
        "{{GENERATED_CONTENT}}": scenario.generated_content,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    if "{{" in template or "}}" in template:
        raise AssertionError("unresolved Task Spec template marker")
    return template


def preflight() -> None:
    if shutil.which("gh") is None:
        raise RuntimeError("gh executable is required")
    if shutil.which("git") is None:
        raise RuntimeError("git executable is required")
    if importlib.util.find_spec("yaml") is None:
        raise RuntimeError("PyYAML is required; install requirements.txt")


def run_dict(run) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "run_attempt": run.attempt,
        "workflow_url": run.workflow_url,
        "sha": run.sha,
        "branch": run.branch,
        "event": run.event,
        "conclusion": run.conclusion,
        "jobs": run.jobs,
    }


def cleanup(
    github: GitHub, scenario: Scenario, pr_number: int | None, keep: bool
) -> tuple[dict[str, Any], list[str]]:
    if keep:
        return {
            "status": "KEPT",
            "pr": pr_number,
            "branches": [scenario.target_branch, scenario.source_branch],
        }, []
    results: dict[str, Any] = {"status": "COMPLETED", "pr": None, "branches": {}}
    errors: list[str] = []
    if pr_number is None:
        try:
            pulls = github.open_pulls(scenario.target_branch, scenario.base_branch)
            if len(pulls) == 1:
                pr_number = int(pulls[0]["number"])
            elif len(pulls) > 1:
                errors.append(f"cleanup found ambiguous PR count: {len(pulls)}")
        except Exception as exc:
            errors.append(f"discover PR for cleanup: {exc}")
    if pr_number is not None:
        try:
            github.close_pr(pr_number)
            results["pr"] = "closed"
        except Exception as exc:
            results["pr"] = "close_failed"
            errors.append(f"close PR #{pr_number}: {exc}")
    for branch in (scenario.target_branch, scenario.source_branch):
        try:
            if github.branch_exists(branch):
                github.delete_branch(branch)
                results["branches"][branch] = "deleted"
            else:
                results["branches"][branch] = "absent"
        except Exception as exc:
            results["branches"][branch] = "delete_failed"
            errors.append(f"delete branch {branch}: {exc}")
    if errors:
        results["status"] = "FAILED"
    return results, errors


def save_report(directory: Path, report: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['scenario_id']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    preflight()
    if not SAFE_REPO.fullmatch(args.repo):
        raise ValueError("--repo must be OWNER/REPO")
    suffix = unique_id(args.unique_id)
    github = GitHub(args.repo)
    try:
        repo_data = github.preflight()
    except Exception as exc:
        failed_scenario = make_scenario(args.repo, suffix)
        failed_report = report_skeleton(failed_scenario)
        failed_report["started_at"] = datetime.now(UTC).isoformat()
        failed_report["completed_at"] = datetime.now(UTC).isoformat()
        failed_report["result"] = "FAIL"
        failed_report["errors"].append(f"GitHub preflight: {type(exc).__name__}: {exc}")
        path = save_report(args.reports_dir.resolve(), failed_report)
        print(f"[FAIL] GitHub preflight\nReport: {path}", file=sys.stderr)
        return 1
    origin_branch = args.base_branch or str(repo_data["default_branch"])
    scenario = make_scenario(args.repo, suffix)
    report = report_skeleton(scenario)
    report["started_at"] = datetime.now(UTC).isoformat()
    report["origin_branch"] = origin_branch
    report["workflow"] = {"file": WORKFLOW_FILE}
    pr_number: int | None = None
    test_passed = False
    cleanup_errors: list[str] = []

    try:
        with tempfile.TemporaryDirectory(prefix="github-pr-e2e-") as temp:
            repository = GitRepository.clone(args.repo, Path(temp) / "repo")
            repository.checkout_base(origin_branch)
            report["source_contracts"] = inspect_source_contracts(repository.root)

            from harness.workflow import choose_trigger, load_workflow, resolve_dispatch_inputs

            workflow_doc = load_workflow(repository.root / ".github" / "workflows" / WORKFLOW_FILE)
            trigger, definitions = choose_trigger(
                workflow_doc, args.trigger, scenario.source_branch, scenario.task_spec
            )
            _, dispatch_definitions = choose_trigger(
                workflow_doc, "workflow_dispatch", scenario.source_branch, scenario.task_spec
            )
            supplied = dispatch_inputs(args.dispatch_input)
            dispatch_inputs_resolved = resolve_dispatch_inputs(
                dispatch_definitions,
                supplied,
                spec_path=scenario.task_spec,
                task_id=scenario.task_id,
            )
            inputs = dispatch_inputs_resolved if trigger == "workflow_dispatch" else {}
            workflow = github.workflow(WORKFLOW_FILE, trigger, definitions)
            report["workflow"].update(
                {
                    "id": workflow.id,
                    "name": workflow.name,
                    "path": workflow.path,
                    "event": trigger,
                    "run2_event": "workflow_dispatch",
                }
            )

            if github.branch_exists(scenario.source_branch) or github.branch_exists(
                scenario.target_branch
            ):
                raise AssertionError("unique E2E branch already exists; choose another --unique-id")
            if github.open_pulls(scenario.target_branch, scenario.base_branch):
                raise AssertionError("unique E2E target already has an open PR")

            repository.create_source_branch(scenario.source_branch)
            repository.write(scenario.task_spec, render_spec(scenario))
            source_sha = repository.commit_spec(
                scenario.task_spec, f"test(e2e): add Phase 5-6 smoke {suffix}"
            )
            report["source_sha"] = source_sha
            repository.push_source(scenario.source_branch)
            if trigger == "workflow_dispatch":
                github.dispatch(workflow.id, scenario.source_branch, inputs)

            discovered = github.discover_unique_run(
                workflow.id,
                scenario.source_branch,
                source_sha,
                trigger,
                args.discovery_timeout_seconds,
            )
            assert_run_identity_count(
                github.matching_runs(workflow.id, scenario.source_branch, source_sha, trigger)
            )
            run1_data = github.wait_attempt(int(discovered["id"]), 1, args.timeout_seconds)
            run1 = github.run_evidence(run1_data)
            assert_successful_run(run1, workflow, scenario, source_sha, 1)

            pulls = github.open_pulls(scenario.target_branch, scenario.base_branch)
            assert len(pulls) == 1, f"expected one open E2E PR, got {len(pulls)}"
            pr1 = github.pr_evidence(pulls[0])
            pr_number = pr1.number
            assert_pr(pr1, scenario)
            branch_sha1 = github.branch_sha(scenario.target_branch)
            assert branch_sha1 == pr1.head_sha
            commit_files = github.commit_files(pr1.head_sha)
            assert_commit_files(commit_files, scenario)
            pull_files = github.pr_files(pr1.number)
            assert_pr_files(pull_files, scenario)
            report["run1"] = {**run_dict(run1), "pr_url": pr1.url, "pr_number": pr1.number}
            report["delivery_commit"] = {"sha": pr1.head_sha, "files": commit_files}
            report["pull_request_files"] = pull_files

            source_tip = github.branch_sha(scenario.source_branch)
            assert_source_tip_matches_run(run1, source_sha, source_tip)
            cancel_result = github.cancel_stuck_run(run1.id, args.discovery_timeout_seconds)
            report["restart_prep"] = {
                "source_tip": source_tip,
                "stuck_run_cancel": cancel_result,
            }

            github.dispatch(workflow.id, scenario.source_branch, dispatch_inputs_resolved)
            discovered2 = github.discover_unique_run(
                workflow.id,
                scenario.source_branch,
                source_sha,
                "workflow_dispatch",
                args.discovery_timeout_seconds,
                exclude_ids={run1.id},
            )
            assert_run_identity_count(
                github.matching_runs(
                    workflow.id,
                    scenario.source_branch,
                    source_sha,
                    "workflow_dispatch",
                    exclude_ids={run1.id},
                )
            )
            run2_data = github.wait_attempt(int(discovered2["id"]), 1, args.timeout_seconds)
            run2 = github.run_evidence(run2_data)
            assert_successful_run(
                run2,
                workflow,
                scenario,
                source_sha,
                1,
                expected_event="workflow_dispatch",
            )
            assert_restart_run(
                run1, run2, expected_event="workflow_dispatch", source_sha=source_sha
            )
            assert_run_identity_count(
                github.matching_runs(
                    workflow.id,
                    scenario.source_branch,
                    source_sha,
                    trigger,
                    exclude_ids={run2.id} if trigger == "workflow_dispatch" else None,
                )
            )
            pulls_after = github.open_pulls(scenario.target_branch, scenario.base_branch)
            assert len(pulls_after) == 1, f"duplicate or missing PR after reuse: {len(pulls_after)}"
            pr2 = github.pr_evidence(pulls_after[0])
            branch_sha2 = github.branch_sha(scenario.target_branch)
            assert_reuse(pr1, pr2, branch_sha2)
            observability, events = github.attempt_events(run2.id, run2.attempt)
            if observability == "observed":
                assert_no_new_delivery_events(events)
            report["run2"] = {
                **run_dict(run2),
                "reused_pr_url": pr2.url,
                "observability": observability,
                "observed_events": events,
            }
            report["pr_count"] = len(pulls_after)
            test_passed = True
    except ProductionGap as exc:
        report["production_gap"] = exc.details
        report["errors"].append(str(exc))
    except Exception as exc:
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    finally:
        cleanup_result, cleanup_errors = cleanup(github, scenario, pr_number, args.keep_resources)
        report["cleanup"] = cleanup_result
        report["cleanup_errors"] = cleanup_errors
        if test_passed and not cleanup_errors:
            report["result"] = "PASS"
        elif test_passed:
            report["result"] = "PASS_CLEANUP_FAILED"
        elif cleanup_errors:
            report["result"] = "FAIL_CLEANUP_FAILED"
        else:
            report["result"] = "FAIL"
        report["completed_at"] = datetime.now(UTC).isoformat()
        path = save_report(args.reports_dir.resolve(), report)
        print(f"[{report['result']}] {scenario.scenario_id}")
        print(f"Report: {path}")
        if report["run1"].get("pr_url"):
            print(f"PR: {report['run1']['pr_url']}")
    if report["result"] == "PASS":
        return 0
    return 2 if test_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
