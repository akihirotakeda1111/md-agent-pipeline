from __future__ import annotations

import argparse
import importlib.util
import json
import re
import secrets
import shutil
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.assertions import (
    assert_comment_did_not_start_review,
    assert_execute_run,
    assert_linear_head_change,
    assert_pr,
    assert_pr_scope,
    assert_review_run,
    assert_tracking_current_head,
    production_terminal_outcome,
    terminal_state,
)
from harness.git import GitRepository
from harness.github import GitHub
from harness.models import (
    ClassifiedFailure,
    E2EBug,
    EnvironmentBlocker,
    ExternalServiceBlocker,
    ProductionBug,
    Scenario,
    report_skeleton,
)
from harness.process import CommandError
from harness.source_contracts import inspect_source_contracts
from harness.workflow import (
    assert_review_workflow_contract,
    choose_execute_trigger,
    load_workflow,
    resolve_dispatch_inputs,
    triggers,
)


SUITE = Path(__file__).resolve().parent
EXECUTE_WORKFLOW = "agent-execute.yml"
REVIEW_WORKFLOW = "agent-review.yml"
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{5,39}$")
SAFE_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TRACKING_MARKER = "<!-- md-agent-review-state"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Real GitHub Phase 7 review E2E suite")
    parser.add_argument("--repo", required=True, help="OWNER/REPO containing Production workflows")
    parser.add_argument("--base-branch", help="Origin branch; defaults to repository default branch")
    parser.add_argument("--unique-id", help="Safe unique suffix; generated when omitted")
    parser.add_argument("--trigger", choices=("auto", "push", "workflow_dispatch"), default="push")
    parser.add_argument(
        "--dispatch-input",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Existing Production workflow_dispatch input; repeatable",
    )
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--execute-timeout-seconds", type=int, default=1800)
    parser.add_argument("--review-timeout-seconds", type=int, default=1800)
    parser.add_argument("--convergence-timeout-seconds", type=int, default=5400)
    parser.add_argument("--discovery-timeout-seconds", type=int, default=240)
    parser.add_argument("--poll-seconds", type=int, default=10)
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
    source_branch = f"e2e/phase7-{suffix}"
    scenario = Scenario(
        scenario_id=f"github-review-e2e-{suffix}",
        repo=repo,
        base_branch=source_branch,
        source_branch=source_branch,
        task_spec=f"specs/tasks/_e2e-phase7-{suffix}.md",
        task_id=f"phase7-e2e-{suffix}",
        target_branch=f"agent/phase7-e2e-{suffix}",
        generated_file=f"app/e2e_phase7_{suffix.replace('-', '_')}.py",
    )
    assert scenario.base_branch == scenario.source_branch
    return scenario


def render_spec(scenario: Scenario) -> str:
    template = (SUITE / "fixtures" / "phase7-e2e-task.md").read_text(encoding="utf-8")
    replacements = {
        "{{UNIQUE_ID}}": scenario.scenario_id.removeprefix("github-review-e2e-"),
        "{{TASK_ID}}": scenario.task_id,
        "{{BASE_BRANCH}}": scenario.base_branch,
        "{{TARGET_BRANCH}}": scenario.target_branch,
        "{{GENERATED_FILE}}": scenario.generated_file,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    if "{{" in template or "}}" in template:
        raise E2EBug("unresolved Task Spec template marker")
    return template


def local_preflight(args: argparse.Namespace) -> None:
    if shutil.which("gh") is None:
        raise EnvironmentBlocker("gh executable is required")
    if shutil.which("git") is None:
        raise EnvironmentBlocker("git executable is required")
    if importlib.util.find_spec("yaml") is None:
        raise EnvironmentBlocker("PyYAML is required; install requirements.txt")
    for name in (
        "execute_timeout_seconds",
        "review_timeout_seconds",
        "convergence_timeout_seconds",
        "discovery_timeout_seconds",
        "poll_seconds",
    ):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} must be positive")


def run_dict(run) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "run_attempt": run.attempt,
        "workflow_url": run.workflow_url,
        "sha": run.sha,
        "branch": run.branch,
        "event": run.event,
        "actor": run.actor,
        "status": run.status,
        "conclusion": run.conclusion,
        "jobs": run.jobs,
        "events": list(run.events),
    }


def feedback_dict(item) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "id": item.source_id,
        "actor": item.actor,
        "path": item.path,
        "commit_sha": item.commit_sha,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "html_url": item.html_url,
    }


def wait_for_one_pr(github: GitHub, scenario: Scenario, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        pulls = github.pulls(scenario.target_branch, scenario.base_branch)
        if len(pulls) > 1:
            raise ProductionBug(f"duplicate E2E PRs created: {len(pulls)}")
        if len(pulls) == 1:
            return github.pr_evidence(pulls[0])
        time.sleep(github.poll_seconds)
    raise ExternalServiceBlocker("Production execute completed but E2E PR was not observed")


def remaining_timeout(deadline: float, configured: int) -> int:
    remaining = int(deadline - time.monotonic())
    if remaining <= 0:
        raise ExternalServiceBlocker("Phase 7 convergence deadline expired")
    return min(configured, remaining)


def ingest_review_run(
    github: GitHub,
    raw_run: dict[str, Any],
    *,
    seen_review_runs: set[int],
    configured_actor: str,
    supported_events: tuple[str, ...],
    timeout_seconds: int,
    report: dict[str, Any],
    all_events: list[str],
):
    run_id = int(raw_run["id"])
    already = run_id in seen_review_runs
    seen_review_runs.add(run_id)
    completed = github.wait_attempt(run_id, timeout_seconds)
    review_run = github.run_evidence(completed)
    assert_review_run(
        review_run,
        configured_actor=configured_actor,
        supported_events=supported_events,
    )
    if not already:
        report["scenario_a"]["review_runs"].append(run_dict(review_run))
        all_events.extend(review_run.events)
    return review_run


def classify_unhandled(exc: BaseException) -> ClassifiedFailure:
    if isinstance(exc, ClassifiedFailure):
        return exc
    if isinstance(exc, AssertionError):
        return ProductionBug(str(exc) or "Production acceptance assertion failed")
    if isinstance(exc, CommandError):
        text = f"{exc.stdout}\n{exc.stderr}".lower()
        if any(token in text for token in ("authentication", "permission", "forbidden", "401", "403")):
            return EnvironmentBlocker(str(exc))
        if any(token in text for token in ("rate limit", "http 5", "timed out", "timeout")):
            return ExternalServiceBlocker(str(exc))
    return E2EBug(f"{type(exc).__name__}: {exc}")


def cleanup(
    github: GitHub, scenario: Scenario, pr_number: int | None, keep: bool
) -> tuple[dict[str, Any], list[str]]:
    if keep:
        return {
            "status": "KEPT",
            "pr": pr_number,
            "branches": [scenario.target_branch, scenario.source_branch],
        }, []
    result: dict[str, Any] = {"status": "COMPLETED", "pr": None, "branches": {}}
    errors: list[str] = []
    if pr_number is None:
        try:
            pulls = github.pulls(scenario.target_branch, scenario.base_branch, state="all")
            if len(pulls) == 1:
                pr_number = int(pulls[0]["number"])
            elif len(pulls) > 1:
                errors.append(f"cleanup found ambiguous PR count: {len(pulls)}")
        except Exception as exc:
            errors.append(f"discover PR for cleanup: {exc}")
    if pr_number is not None:
        try:
            current = github.pr_evidence(pr_number)
            if current.state == "open":
                github.close_pr(pr_number)
                result["pr"] = "closed"
            else:
                result["pr"] = current.state
        except Exception as exc:
            result["pr"] = "close_failed"
            errors.append(f"close PR #{pr_number}: {exc}")
    for branch in (scenario.target_branch, scenario.source_branch):
        try:
            if github.branch_exists(branch):
                github.delete_branch(branch)
                result["branches"][branch] = "deleted"
            else:
                result["branches"][branch] = "absent"
        except Exception as exc:
            result["branches"][branch] = "delete_failed"
            errors.append(f"delete branch {branch}: {exc}")
    if errors:
        result["status"] = "FAILED"
    return result, errors


def save_report(directory: Path, report: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report['scenario_id']}.json"
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def main() -> int:
    args = parse_args()
    suffix = unique_id(args.unique_id)
    if not SAFE_REPO.fullmatch(args.repo):
        raise ValueError("--repo must be OWNER/REPO")
    scenario = make_scenario(args.repo, suffix)
    report = report_skeleton(scenario)
    report["started_at"] = datetime.now(UTC).isoformat()
    github = GitHub(args.repo, poll_seconds=args.poll_seconds)
    pr_number: int | None = None
    test_passed = False
    preflight_only_passed = False
    resources_created = False

    try:
        local_preflight(args)
        repo_data = github.preflight()
        origin_branch = args.base_branch or str(repo_data["default_branch"])
        viewer_login = str(repo_data["viewer_login"])
        report["origin_branch"] = origin_branch

        with tempfile.TemporaryDirectory(prefix="github-review-e2e-") as temp:
            repository = GitRepository.clone(args.repo, Path(temp) / "repo")
            repository.checkout_base(origin_branch)
            contracts = inspect_source_contracts(
                repository.root,
                e2e_base_branch=scenario.base_branch,
                default_branch=str(repo_data["default_branch"]),
            )
            configured_actor = str(contracts["coderabbit_actor"])
            configured_check_app_slug = str(contracts["coderabbit_check_app_slug"])
            configured_status_context = str(contracts["coderabbit_status_context"])
            if viewer_login == configured_actor:
                raise EnvironmentBlocker(
                    "Harness GitHub actor must differ from coderabbit.actor for Scenario B"
                )

            execute_doc = load_workflow(
                repository.root / ".github" / "workflows" / EXECUTE_WORKFLOW
            )
            review_doc = load_workflow(
                repository.root / ".github" / "workflows" / REVIEW_WORKFLOW
            )
            trigger, trigger_definitions = choose_execute_trigger(
                execute_doc, args.trigger, scenario.source_branch, scenario.task_spec
            )
            dispatch_definitions = triggers(execute_doc).get("workflow_dispatch", {})
            dispatch_definitions = (
                dispatch_definitions.get("inputs", {})
                if isinstance(dispatch_definitions, dict)
                else {}
            )
            resolved_inputs = resolve_dispatch_inputs(
                dispatch_definitions,
                dispatch_inputs(args.dispatch_input),
                spec_path=scenario.task_spec,
                task_id=scenario.task_id,
            )
            if trigger == "workflow_dispatch" and not trigger_definitions:
                trigger_definitions = dispatch_definitions
            review_events = assert_review_workflow_contract(review_doc)
            execute_workflow = github.workflow(EXECUTE_WORKFLOW, (trigger,))
            review_workflow = github.workflow(REVIEW_WORKFLOW, review_events)
            report["preflight"] = {
                "status": "PASS",
                "viewer_login": viewer_login,
                "source_contracts": contracts,
                "execute_workflow": {
                    "id": execute_workflow.id,
                    "name": execute_workflow.name,
                    "path": execute_workflow.path,
                    "trigger": trigger,
                },
                "review_workflow": {
                    "id": review_workflow.id,
                    "name": review_workflow.name,
                    "path": review_workflow.path,
                    "events": list(review_events),
                },
                "manual_preconditions_not_secret-read": [
                    "CODEX_API_KEY",
                    "REVIEW_CLASSIFIER_API_KEY",
                    "CodeRabbit App authorization and auto/incremental review",
                    "automatic merge disabled",
                ],
            }
            if args.preflight_only:
                preflight_only_passed = True
                return 0

            if github.branch_exists(scenario.source_branch) or github.branch_exists(
                scenario.target_branch
            ):
                raise EnvironmentBlocker("unique E2E branch already exists; choose another ID")
            if github.pulls(scenario.target_branch, scenario.base_branch, state="all"):
                raise EnvironmentBlocker("unique E2E target already has a PR")

            review_baseline = github.workflow_run_ids(review_workflow.id)
            seen_review_runs = set(review_baseline)
            repository.create_source_branch(scenario.source_branch)
            repository.write(scenario.task_spec, render_spec(scenario))
            source_sha = repository.commit_spec(
                scenario.task_spec, f"test(e2e): add Phase 7 review scenario {suffix}"
            )
            repository.push_source(scenario.source_branch)
            resources_created = True
            if trigger == "workflow_dispatch":
                github.dispatch(execute_workflow.id, scenario.source_branch, resolved_inputs)

            discovered = github.discover_unique_execute_run(
                execute_workflow.id,
                scenario.source_branch,
                source_sha,
                trigger,
                args.discovery_timeout_seconds,
            )
            execute_data = github.wait_attempt(int(discovered["id"]), args.execute_timeout_seconds)
            execute_run = github.run_evidence(execute_data)
            assert_execute_run(
                execute_run, execute_workflow, scenario, source_sha, trigger
            )
            report["execute"] = run_dict(execute_run)

            pr = wait_for_one_pr(github, scenario, args.discovery_timeout_seconds)
            pr_number = pr.number
            assert_pr(pr, scenario)
            assert_pr_scope(github.pr_files(pr.number), scenario)
            initial_head = pr.head_sha
            current_head = initial_head
            report["scenario_a"]["pr_url"] = pr.url
            report["scenario_a"]["pr_number"] = pr.number
            report["scenario_a"]["initial_head_sha"] = initial_head
            report["scenario_a"]["head_history"].append(initial_head)

            known_feedback: set[tuple[str, int, str]] = set()
            convergence_deadline = time.monotonic() + args.convergence_timeout_seconds
            all_events: list[str] = []
            scenario_a_state: str | None = None
            scenario_a_terminal: dict[str, str] | None = None
            latest_review_run = None
            while True:
                signal = github.wait_for_scenario_a_signal(
                    pr_number=pr.number,
                    head_sha=current_head,
                    actor=configured_actor,
                    check_app_slug=configured_check_app_slug,
                    status_context=configured_status_context,
                    known_ids=known_feedback,
                    workflow_id=review_workflow.id,
                    baseline_ids=seen_review_runs,
                    timeout_seconds=remaining_timeout(
                        convergence_deadline, args.review_timeout_seconds
                    ),
                    correlation_text=suffix,
                )
                if signal.get("coderabbit_terminal"):
                    scenario_a_terminal = signal["coderabbit_terminal"]
                if signal["kind"] == "feedback":
                    for item in signal["items"]:
                        identity = (item.kind, item.source_id, item.updated_at)
                        if identity not in known_feedback:
                            report["scenario_a"]["feedback"].append(feedback_dict(item))
                            known_feedback.add(identity)
                    continue

                raw_run = signal.get("run")
                if raw_run is not None:
                    latest_review_run = ingest_review_run(
                        github,
                        raw_run,
                        seen_review_runs=seen_review_runs,
                        configured_actor=configured_actor,
                        supported_events=review_events,
                        timeout_seconds=remaining_timeout(
                            convergence_deadline, args.review_timeout_seconds
                        ),
                        report=report,
                        all_events=all_events,
                    )

                updated = github.pr_evidence(pr.number)
                assert not updated.merged and updated.auto_merge is None
                if updated.head_sha != current_head:
                    if latest_review_run is None:
                        raise ProductionBug(
                            "PR HEAD changed before a review run was observed",
                            evidence={"before": current_head, "after": updated.head_sha},
                        )
                    comparison = github.compare(current_head, updated.head_sha)
                    assert_linear_head_change(comparison, scenario)
                    assert github.commit_files(updated.head_sha) == [scenario.generated_file]
                    assert "REVIEW_FIX_STARTED" in latest_review_run.events
                    assert "REVIEW_FIX_VALIDATION_PASSED" in latest_review_run.events
                    report["scenario_a"]["repairs"] = report["scenario_a"].get("repairs", [])
                    report["scenario_a"]["repairs"].append(
                        {
                            "before": current_head,
                            "after": updated.head_sha,
                            "ahead_by": comparison.get("ahead_by"),
                            "run_id": latest_review_run.id,
                        }
                    )
                    current_head = updated.head_sha
                    report["scenario_a"]["head_history"].append(current_head)
                    continue

                outcome = production_terminal_outcome(
                    updated, latest_review_run, current_head=current_head
                )
                if outcome == "FAILED":
                    raise ProductionBug(
                        "Scenario A reached Production FAILED on the current HEAD",
                        evidence={
                            "labels": list(updated.labels),
                            "events": all_events,
                            "coderabbit_terminal": scenario_a_terminal,
                        },
                    )
                if outcome in {"READY_FOR_HUMAN", "ESCALATED"}:
                    pr = updated
                    scenario_a_state = outcome
                    break
                if (
                    latest_review_run is not None
                    and latest_review_run.conclusion == "failure"
                ):
                    raise ProductionBug(
                        "review workflow failed without a Production READY/ESCALATED terminal",
                        evidence={
                            "labels": list(updated.labels),
                            "events": all_events,
                            "coderabbit_terminal": scenario_a_terminal,
                        },
                    )
                if signal["kind"] == "production_terminal":
                    time.sleep(github.poll_seconds)

            if scenario_a_state is None:
                raise ProductionBug("Scenario A ended without a Production terminal outcome")
            if scenario_a_terminal is None:
                scenario_a_terminal = github.coderabbit_terminal(
                    current_head,
                    actor=configured_actor,
                    check_app_slug=configured_check_app_slug,
                    status_context=configured_status_context,
                )
            if scenario_a_state == "READY_FOR_HUMAN":
                required_events = {"REVIEW_RECEIVED", "REVIEW_COLLECTED", "READY_FOR_HUMAN"}
                if report["scenario_a"]["feedback"]:
                    required_events.update({"REVIEW_CLASSIFIED", "REVIEW_POLICY_APPLIED"})
            else:
                required_events = {"REVIEW_RECEIVED", "REVIEW_ESCALATED"}
            missing_events = sorted(required_events - set(all_events))
            if missing_events:
                raise ProductionBug(
                    f"Scenario A structured event(s) not observable: {missing_events}"
                )
            if scenario_a_state == "READY_FOR_HUMAN":
                tracking = github.tracking_comments(pr.number, TRACKING_MARKER)
                assert_tracking_current_head(tracking, scenario, current_head)
                tracking_id = tracking[0].get("id")
            else:
                tracking_id = None
            repair_count = len(report["scenario_a"].get("repairs", []))
            report["scenario_a"].update(
                {
                    "final_state": scenario_a_state,
                    "coderabbit_terminal": scenario_a_terminal,
                    "final_head_sha": current_head,
                    "current_head_feedback_converged": scenario_a_state == "READY_FOR_HUMAN",
                    "actionable_repair_observed": repair_count > 0,
                    "repair_count": repair_count,
                    "tracking_comment_id": tracking_id,
                    "automatic_merge": pr.auto_merge,
                    "merged": pr.merged,
                }
            )

            # Scenario B posts a human PR comment. That is an instruction to
            # CodeRabbit or a no-op for Agent Review; it must not start this workflow.
            b_baseline = github.workflow_run_ids(review_workflow.id)
            b_head = current_head
            b_state = scenario_a_state
            marker = f"<!-- phase7-e2e-non-coderabbit:{suffix} -->"
            wakeup = github.create_non_coderabbit_wakeup(
                pr.number,
                marker=marker,
            )
            observed_b = github.wait_without_comment_review_run(
                review_workflow.id,
                b_baseline,
                timeout_seconds=args.discovery_timeout_seconds,
            )
            assert_comment_did_not_start_review(observed_b)
            after_b = github.pr_evidence(pr.number)
            assert after_b.head_sha == b_head, "human comment changed PR HEAD"
            assert terminal_state(after_b) == b_state
            after_terminal = github.coderabbit_terminal(
                after_b.head_sha,
                actor=configured_actor,
                check_app_slug=configured_check_app_slug,
                status_context=configured_status_context,
            )
            assert not after_b.merged and after_b.auto_merge is None
            report["scenario_b"].update(
                {
                    "status": "PASS",
                    "wakeup": wakeup,
                    "actor": viewer_login,
                    "run": None,
                    "observed_runs": [
                        {
                            "id": item.get("id"),
                            "event": item.get("event"),
                            "html_url": item.get("html_url"),
                        }
                        for item in observed_b
                    ],
                    "head_before": b_head,
                    "head_after": after_b.head_sha,
                    "terminal_state_after": terminal_state(after_b),
                    "coderabbit_terminal_after": after_terminal,
                }
            )

            all_pulls = github.pulls(scenario.target_branch, scenario.base_branch, state="all")
            if len(all_pulls) != 1:
                raise ProductionBug(f"same work-unit PR count must remain 1, got {len(all_pulls)}")
            final_pr = github.pr_evidence(pr.number)
            assert_pr_scope(github.pr_files(pr.number), scenario)
            assert final_pr.state == "open"
            assert not final_pr.merged and final_pr.auto_merge is None
            report["pr_count"] = len(all_pulls)
            report["final_pr"] = {
                "url": final_pr.url,
                "number": final_pr.number,
                "head_sha": final_pr.head_sha,
                "labels": list(final_pr.labels),
                "merged": final_pr.merged,
                "auto_merge": final_pr.auto_merge,
            }
            test_passed = True
    except BaseException as exc:
        failure = classify_unhandled(exc)
        report["blocker_or_failure"] = {
            "category": failure.category,
            "message": str(failure),
            "evidence": failure.evidence,
        }
        report["errors"].append(f"{failure.category}: {failure}")
    finally:
        cleanup_errors: list[str] = []
        if resources_created:
            cleanup_result, cleanup_errors = cleanup(
                github, scenario, pr_number, args.keep_resources
            )
        else:
            cleanup_result = {"status": "NOT_NEEDED"}
        report["cleanup"] = cleanup_result
        report["cleanup_errors"] = cleanup_errors
        if preflight_only_passed:
            report["result"] = "PASS_PREFLIGHT"
        elif test_passed and not cleanup_errors:
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
        pr_url = report.get("scenario_a", {}).get("pr_url")
        if pr_url:
            print(f"PR: {pr_url}")
    if report["result"] == "PASS":
        return 0
    return 2 if test_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
