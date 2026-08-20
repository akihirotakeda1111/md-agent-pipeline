from __future__ import annotations

import inspect
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "yaml" not in sys.modules:
    sys.modules["yaml"] = types.SimpleNamespace(
        safe_load=json.loads, YAMLError=ValueError
    )

import run as runner
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
from harness.coderabbit_terminal import (
    KIND_SKIPPED,
    bind_observed_case,
    load_observed_cases,
    resolve_coderabbit_terminal,
)
from harness.git import GitRepository
from harness.github import (
    GitHub,
    CANDIDATE_OTHER_PR,
    CANDIDATE_PREPARE_FAILED,
    CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE,
    CANDIDATE_PREPARE_SKIPPED,
    CANDIDATE_REVIEW_EXECUTED,
    CANDIDATE_REVIEW_PENDING,
    CANDIDATE_STALE_HEAD,
    candidate_evidence_row,
    choose_unseen_review_run,
    classify_terminal_wake_candidate,
    completed_review_run_for_terminal,
    new_terminal_wake_runs,
    prepare_binds_to_target_pr,
    raise_if_prepare_fault,
)
from harness.models import (
    EnvironmentBlocker,
    ProductionBug,
    PullRequestEvidence,
    RunEvidence,
    WorkflowInfo,
    report_skeleton,
)
from harness.source_contracts import REQUIRED_FILES, inspect_source_contracts
from harness.workflow import (
    assert_review_workflow_contract,
    choose_execute_trigger,
    matches_filters,
    resolve_dispatch_inputs,
)


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = runner.make_scenario("owner/repo", "20260819-abcdef")

    def test_rendered_spec_is_phase7_isolated_and_validatable(self) -> None:
        rendered = runner.render_spec(self.scenario)
        self.assertNotIn("{{", rendered)
        self.assertIn(self.scenario.task_id, rendered)
        self.assertIn(self.scenario.generated_file, rendered)
        self.assertIn("review_attempt_limit: 3", rendered)
        self.assertIn("### Requirement", rendered)
        self.assertIn(".agent/state/**", rendered)
        self.assertNotIn("automatic merge", rendered.lower())

    def test_scenario_names_are_safely_prefixed(self) -> None:
        self.assertTrue(self.scenario.source_branch.startswith("e2e/phase7-"))
        self.assertTrue(self.scenario.target_branch.startswith("agent/phase7-e2e-"))
        self.assertTrue(self.scenario.task_spec.startswith("specs/tasks/_e2e-phase7-"))
        self.assertTrue(self.scenario.generated_file.startswith("app/e2e_phase7_"))
        self.assertEqual(self.scenario.base_branch, self.scenario.source_branch)

    def test_execute_trigger_and_dispatch_reuse_phase6_pattern(self) -> None:
        workflow = {"on": {"push": {"paths": ["specs/tasks/**/*.md"]}, "workflow_dispatch": {}}}
        trigger, _ = choose_execute_trigger(
            workflow, "auto", self.scenario.source_branch, self.scenario.task_spec
        )
        self.assertEqual(trigger, "push")
        definitions = {"spec_path": {"required": True}, "mode": {"default": "normal"}}
        values = resolve_dispatch_inputs(
            definitions, {}, spec_path=self.scenario.task_spec, task_id=self.scenario.task_id
        )
        self.assertEqual(values, {"spec_path": self.scenario.task_spec, "mode": "normal"})

    def test_github_style_filters_handle_include_exclude_order(self) -> None:
        self.assertTrue(matches_filters("e2e/phase7-id", ["e2e/**"], None))
        self.assertFalse(
            matches_filters("e2e/private/id", ["e2e/**", "!e2e/private/**"], None)
        )
        self.assertTrue(
            matches_filters("specs/tasks/_e2e-phase7-id.md", ["specs/tasks/**/*.md"], None)
        )

    def test_review_workflow_contract_is_async_and_bounded(self) -> None:
        workflow = {
            "on": {
                "check_run": {"types": ["completed"]},
                "status": None,
            },
            "jobs": {
                "prepare": {},
                "review": {
                    "concurrency": {
                        "group": "${{ github.workflow }}-${{ needs.prepare.outputs.pull_number }}",
                        "cancel-in-progress": False,
                    }
                },
            },
        }
        self.assertEqual(
            assert_review_workflow_contract(workflow),
            ("check_run", "status"),
        )

    def test_review_workflow_contract_rejects_workflow_level_concurrency(self) -> None:
        workflow = {
            "on": {
                "check_run": {"types": ["completed"]},
                "status": None,
            },
            "concurrency": {"group": "agent-review-pr", "cancel-in-progress": False},
            "jobs": {
                "prepare": {},
                "review": {
                    "concurrency": {
                        "group": "${{ github.workflow }}-${{ needs.prepare.outputs.pull_number }}",
                        "cancel-in-progress": False,
                    }
                },
            },
        }
        with self.assertRaises(ProductionBug):
            assert_review_workflow_contract(workflow)

    def test_review_workflow_contract_requires_terminal_wakeups(self) -> None:
        workflow = {
            "on": {"issue_comment": {"types": ["created"]}},
            "concurrency": {"group": "agent-review-pr", "cancel-in-progress": False},
            "jobs": {},
        }
        with self.assertRaises(ProductionBug):
            assert_review_workflow_contract(workflow)

    def test_review_workflow_contract_rejects_comment_wakeups(self) -> None:
        workflow = {
            "on": {
                "issue_comment": {"types": ["created"]},
                "check_run": {"types": ["completed"]},
                "status": None,
            },
            "jobs": {
                "prepare": {},
                "review": {
                    "concurrency": {
                        "group": "${{ github.workflow }}-${{ needs.prepare.outputs.pull_number }}",
                        "cancel-in-progress": False,
                    }
                },
            },
        }
        with self.assertRaises(ProductionBug):
            assert_review_workflow_contract(workflow)

    def test_harness_mapper_matches_shared_observed_cases(self) -> None:
        payload = load_observed_cases()
        identity = payload["identity"]
        for case in payload["cases"]:
            with self.subTest(case["id"]):
                checks, statuses, head = bind_observed_case(case)
                first = resolve_coderabbit_terminal(
                    checks,
                    statuses,
                    head_sha=head,
                    actor=identity["actor"],
                    check_app_slug=identity["check_app_slug"],
                    status_context=identity["status_context"],
                )
                second = resolve_coderabbit_terminal(
                    checks,
                    statuses,
                    head_sha=head,
                    actor=identity["actor"],
                    check_app_slug=identity["check_app_slug"],
                    status_context=identity["status_context"],
                )
                expected = case["expected"]
                self.assertEqual(first["kind"], expected["kind"])
                self.assertEqual(first.get("source"), expected.get("source", first.get("source")))
                self.assertEqual(
                    first.get("description"),
                    expected.get("description", first.get("description")),
                )
                self.assertEqual(first, second)
                if expected["kind"] == KIND_SKIPPED:
                    self.assertNotEqual(expected.get("outcome"), "READY_FOR_HUMAN")
                    self.assertEqual(expected.get("outcome"), "ESCALATED")

    def test_run_pr_review_and_scope_assertions(self) -> None:
        execute_workflow = WorkflowInfo(
            1,
            "Agent Execute",
            ".github/workflows/agent-execute.yml",
            "active",
            ("push",),
        )
        execute = RunEvidence(
            10,
            1,
            "https://example.invalid/runs/10",
            "a" * 40,
            self.scenario.source_branch,
            "push",
            "human",
            "completed",
            "success",
            {"Parse spec": "success", "Execute task": "success", "Deliver commit": "success"},
        )
        assert_execute_run(
            execute, execute_workflow, self.scenario, "a" * 40, "push"
        )
        body = "\n".join(
            [
                f"<!-- agent-work-unit: {self.scenario.task_id} -->",
                "Task Spec",
                "Objective",
                "Completed Tasks",
                "Changed Files",
                "Validation Results",
                "Final Verification",
                "Repair Attempts",
            ]
        )
        pr = PullRequestEvidence(
            4,
            "https://example.invalid/pull/4",
            self.scenario.target_branch,
            self.scenario.base_branch,
            "b" * 40,
            body,
            ("agent:review",),
            "open",
            False,
            None,
            None,
        )
        assert_pr(pr, self.scenario)
        assert_pr_scope([self.scenario.generated_file], self.scenario)

    def test_review_and_negative_actor_run_boundaries(self) -> None:
        actor = "configured-review-bot"
        events = ("check_run", "status")
        check_run = RunEvidence(
            13,
            1,
            "https://example.invalid/runs/13",
            "b" * 40,
            self.scenario.target_branch,
            "check_run",
            "github-actions[bot]",
            "completed",
            "success",
            {"Prepare review": "success", "Review and repair": "success"},
            ("REVIEW_RECEIVED", "READY_FOR_HUMAN"),
        )
        assert_review_run(check_run, configured_actor=actor, supported_events=events)
        escalated = RunEvidence(
            14,
            1,
            "https://example.invalid/runs/14",
            "b" * 40,
            self.scenario.target_branch,
            "check_run",
            "github-actions[bot]",
            "completed",
            "failure",
            {"Prepare review": "success", "Review and repair": "failure"},
            ("REVIEW_RECEIVED", "REVIEW_ESCALATED"),
        )
        assert_review_run(escalated, configured_actor=actor, supported_events=events)
        comment_run = {
            "id": 12,
            "event": "issue_comment",
            "html_url": "https://example.invalid/runs/12",
        }
        with self.assertRaises(AssertionError):
            assert_comment_did_not_start_review([comment_run])
        assert_comment_did_not_start_review([{"id": 15, "event": "check_run"}])

    def test_terminal_state_and_linear_comparison_are_observable_only(self) -> None:
        pr = PullRequestEvidence(
            4,
            "url",
            self.scenario.target_branch,
            self.scenario.base_branch,
            "b" * 40,
            "",
            ("agent:ready",),
            "open",
            False,
            None,
            None,
        )
        self.assertEqual(terminal_state(pr), "READY_FOR_HUMAN")
        ready_run = RunEvidence(
            20,
            1,
            "https://example.invalid/runs/20",
            "b" * 40,
            self.scenario.target_branch,
            "check_run",
            "github-actions[bot]",
            "completed",
            "success",
            {"Prepare review": "success", "Review and repair": "success"},
            ("REVIEW_RECEIVED", "REVIEW_COLLECTED", "READY_FOR_HUMAN"),
        )
        self.assertEqual(
            production_terminal_outcome(pr, ready_run, current_head="b" * 40),
            "READY_FOR_HUMAN",
        )
        stale_ready = PullRequestEvidence(
            4,
            "url",
            self.scenario.target_branch,
            self.scenario.base_branch,
            "b" * 40,
            "",
            ("agent:ready",),
            "open",
            False,
            None,
            None,
        )
        self.assertIsNone(
            production_terminal_outcome(stale_ready, ready_run, current_head="c" * 40)
        )
        escalated_pr = PullRequestEvidence(
            4,
            "url",
            self.scenario.target_branch,
            self.scenario.base_branch,
            "b" * 40,
            "",
            ("agent:escalated",),
            "open",
            False,
            None,
            None,
        )
        escalated_run = RunEvidence(
            21,
            1,
            "https://example.invalid/runs/21",
            "b" * 40,
            self.scenario.target_branch,
            "check_run",
            "github-actions[bot]",
            "completed",
            "failure",
            {"Prepare review": "success", "Review and repair": "failure"},
            ("REVIEW_RECEIVED", "REVIEW_ESCALATED"),
        )
        self.assertEqual(
            production_terminal_outcome(escalated_pr, escalated_run, current_head="b" * 40),
            "ESCALATED",
        )
        assert_linear_head_change(
            {
                "status": "ahead",
                "behind_by": 0,
                "ahead_by": 1,
                "files": [{"filename": self.scenario.generated_file}],
            },
            self.scenario,
        )
        assert_tracking_current_head(
            [{"body": f"spec_id: {self.scenario.task_id}\nhead_sha: {'b' * 40}"}],
            self.scenario,
            "b" * 40,
        )

    def test_source_contracts_read_configured_actor_without_hardcoding(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative in REQUIRED_FILES:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("contract\n", encoding="utf-8")
            (root / "agent" / "config.json").write_text(
                json.dumps(
                    {
                        "coderabbit": {
                            "actor": "observed-review-actor",
                            "check_app_slug": "observed-app",
                            "status_context": "ObservedRabbit",
                        },
                        "review": {
                            "classifier_model": "pinned-model-snapshot",
                            "track_author": "orchestrator-bot",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (root / ".coderabbit.yaml").write_text(
                json.dumps(
                    {
                        "reviews": {
                            "auto_review": {
                                "enabled": True,
                                "auto_incremental_review": True,
                                "base_branches": ["e2e/phase7-.*"],
                            },
                            "review_status": True,
                            "review_progress": True,
                            "commit_status": True,
                            "finishing_touches": {"autofix": {"enabled": False}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = inspect_source_contracts(root)
            self.assertEqual(result["coderabbit_actor"], "observed-review-actor")
            self.assertEqual(result["coderabbit_check_app_slug"], "observed-app")
            self.assertEqual(result["coderabbit_status_context"], "ObservedRabbit")
            self.assertTrue(result["coderabbit"]["commit_status"])
            self.assertIn(".agent/phases/07-coderabbit-review.md", result["files"])

    def test_report_has_scenarios_cleanup_and_classification_fields(self) -> None:
        report = report_skeleton(self.scenario)
        for key in (
            "scenario_a",
            "scenario_b",
            "pr_count",
            "cleanup",
            "blocker_or_failure",
            "result",
        ):
            self.assertIn(key, report)
        self.assertIn("terminal_transports", report["scenario_a"])
        with tempfile.TemporaryDirectory() as temp:
            saved = runner.save_report(Path(temp), report)
            self.assertEqual(json.loads(saved.read_text())["scenario_id"], report["scenario_id"])

    def test_harness_has_no_fake_or_production_policy_implementation(self) -> None:
        sources = inspect.getsource(runner) + inspect.getsource(GitHub) + inspect.getsource(GitRepository)
        for forbidden in ("FakeGitHub", "FakeCodex", "SemanticClassifier", "PolicyEngine"):
            self.assertNotIn(forbidden, sources)
        self.assertNotIn("gh secret", sources.lower())
        self.assertNotIn("/actions/secrets", sources.lower())

    def test_git_helper_has_no_history_rewrite_or_merge(self) -> None:
        source = inspect.getsource(GitRepository).lower()
        for forbidden in ("--force", "--amend", "rebase", "merge", "reset --hard"):
            self.assertNotIn(forbidden, source)

    def test_polling_helpers_have_explicit_deadlines(self) -> None:
        source = inspect.getsource(GitHub)
        self.assertIn("time.monotonic() + timeout_seconds", source)
        self.assertNotIn("while True", source)

    def test_scenario_a_poll_uses_production_terminal_not_coderabbit_mapper(self) -> None:
        wait_source = inspect.getsource(GitHub.wait_for_scenario_a_signal)
        self.assertIn("production_terminal", wait_source)
        self.assertNotIn("KIND_COMPLETED", wait_source)
        self.assertNotIn("KIND_SKIPPED", wait_source)
        runner_source = inspect.getsource(runner)
        self.assertIn("production_terminal_outcome", runner_source)
        self.assertNotIn("KIND_COMPLETED", runner_source)
        self.assertNotIn("KIND_SKIPPED", runner_source)

    def test_review_run_correlation_uses_prepare_not_run_head_or_title(self) -> None:
        wait_source = inspect.getsource(GitHub.wait_for_scenario_a_signal)
        classify_source = inspect.getsource(classify_terminal_wake_candidate)
        helper_source = inspect.getsource(prepare_binds_to_target_pr)
        runner_source = inspect.getsource(runner)
        combined = wait_source + classify_source + helper_source
        self.assertNotIn("correlation_text", combined)
        self.assertNotIn("display_title", combined)
        self.assertNotIn("correlation_text", runner_source)
        self.assertIn("pull_number", helper_source)
        self.assertIn("head_sha", helper_source)
        self.assertIn("review_baseline", runner_source)
        self.assertIn("seen_ids", wait_source)
        self.assertIn("raise_if_prepare_fault", wait_source)
        timeout_source = inspect.getsource(GitHub.scenario_a_timeout_evidence)
        self.assertIn("candidate_runs", timeout_source)
        self.assertIn("prepare_results", timeout_source)
        self.assertIn("check_run_status_history", timeout_source)

    def test_prepare_output_binds_pr_even_when_run_head_is_default_branch(self) -> None:
        pr_head = "b" * 40
        default_branch = "c" * 40
        prepare = {
            "should_review": True,
            "pull_number": 19,
            "head_sha": pr_head,
            "reason": "ok",
        }
        self.assertTrue(
            prepare_binds_to_target_pr(prepare, pr_number=19, head_sha=pr_head)
        )
        self.assertFalse(
            prepare_binds_to_target_pr(
                {"should_review": True, "pull_number": 19, "head_sha": default_branch},
                pr_number=19,
                head_sha=pr_head,
            )
        )
        runs = [
            {"id": 99, "event": "status", "head_sha": default_branch},
            {"id": 2, "event": "status", "head_sha": default_branch},
            {"id": 3, "event": "check_run", "head_sha": default_branch},
            {"id": 4, "event": "issue_comment", "head_sha": pr_head},
        ]
        candidates = new_terminal_wake_runs(runs, {99})
        self.assertEqual([int(item["id"]) for item in candidates], [2, 3])
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare=prepare,
                jobs={"Prepare review": "success", "Review and repair": "success"},
                run_status="completed",
                pr_number=19,
                head_sha=pr_head,
            ),
            CANDIDATE_REVIEW_EXECUTED,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": False,
                    "pull_number": 19,
                    "head_sha": pr_head,
                    "reason": "event sha is not the current pull head",
                },
                jobs={"Prepare review": "success", "Review and repair": "skipped"},
                run_status="completed",
                pr_number=19,
                head_sha=pr_head,
            ),
            CANDIDATE_PREPARE_SKIPPED,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": True,
                    "pull_number": "19",
                    "head_sha": pr_head,
                },
                jobs={},
                run_status="in_progress",
                pr_number=19,
                head_sha=pr_head,
            ),
            CANDIDATE_REVIEW_PENDING,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": True,
                    "pull_number": 20,
                    "head_sha": pr_head,
                },
                jobs={"Prepare review": "success", "Review and repair": "success"},
                run_status="completed",
                pr_number=19,
                head_sha=pr_head,
            ),
            CANDIDATE_OTHER_PR,
        )

    def test_prepare_failure_is_not_a_normal_skip(self) -> None:
        current = "b" * 40
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare=None,
                jobs={"Prepare review": "failure"},
                run_status="completed",
                pr_number=19,
                head_sha=current,
            ),
            CANDIDATE_PREPARE_FAILED,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": False,
                    "pull_number": 19,
                    "head_sha": current,
                    "reason": "event sha is not the current pull head",
                },
                jobs={"Prepare review": "success", "Review and repair": "skipped"},
                run_status="completed",
                pr_number=19,
                head_sha=current,
            ),
            CANDIDATE_PREPARE_SKIPPED,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare=None,
                jobs={"Prepare review": "success"},
                run_status="completed",
                pr_number=19,
                head_sha=current,
                prepare_log_error=True,
            ),
            CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE,
        )
        with self.assertRaises(ProductionBug) as failed:
            raise_if_prepare_fault(
                {
                    "kind": CANDIDATE_PREPARE_FAILED,
                    "id": 7,
                    "event": "status",
                    "jobs": {"Prepare review": "failure"},
                }
            )
        self.assertEqual(failed.exception.category, "PRODUCTION_BUG")
        with self.assertRaises(EnvironmentBlocker) as missing_logs:
            raise_if_prepare_fault(
                {
                    "kind": CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE,
                    "id": 8,
                    "event": "check_run",
                    "prepare_log_error": True,
                    "jobs": {"Prepare review": "success"},
                }
            )
        self.assertEqual(missing_logs.exception.category, "ENVIRONMENT_BLOCKER")
        with self.assertRaises(ProductionBug):
            raise_if_prepare_fault(
                {
                    "kind": CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE,
                    "id": 9,
                    "event": "status",
                    "jobs": {"Prepare review": "success"},
                }
            )
        raise_if_prepare_fault({"kind": CANDIDATE_PREPARE_SKIPPED, "id": 10})

    def test_repair_head_and_old_head_terminal_are_separated(self) -> None:
        old_head = "a" * 40
        new_head = "b" * 40
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": True,
                    "pull_number": 19,
                    "head_sha": new_head,
                    "reason": "ok",
                },
                jobs={"Prepare review": "success", "Review and repair": "success"},
                run_status="completed",
                pr_number=19,
                head_sha=new_head,
            ),
            CANDIDATE_REVIEW_EXECUTED,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": True,
                    "pull_number": 19,
                    "head_sha": old_head,
                    "reason": "ok",
                },
                jobs={"Prepare review": "success", "Review and repair": "success"},
                run_status="completed",
                pr_number=19,
                head_sha=new_head,
            ),
            CANDIDATE_STALE_HEAD,
        )
        self.assertEqual(
            classify_terminal_wake_candidate(
                prepare={
                    "should_review": False,
                    "pull_number": 19,
                    "head_sha": new_head,
                    "reason": "event sha is not the current pull head",
                },
                jobs={"Prepare review": "success", "Review and repair": "skipped"},
                run_status="completed",
                pr_number=19,
                head_sha=new_head,
            ),
            CANDIDATE_PREPARE_SKIPPED,
        )
        classified = [
            {
                "kind": CANDIDATE_STALE_HEAD,
                "run": {
                    "id": 11,
                    "event": "status",
                    "status": "completed",
                    "created_at": "2026-08-20T00:00:00Z",
                },
            },
            {
                "kind": CANDIDATE_PREPARE_SKIPPED,
                "run": {
                    "id": 12,
                    "event": "check_run",
                    "status": "completed",
                    "created_at": "2026-08-20T00:01:00Z",
                },
            },
            {
                "kind": CANDIDATE_REVIEW_EXECUTED,
                "run": {
                    "id": 13,
                    "event": "status",
                    "status": "completed",
                    "created_at": "2026-08-20T00:02:00Z",
                },
            },
        ]
        chosen = choose_unseen_review_run(classified, seen_ids=set())
        self.assertIsNotNone(chosen)
        self.assertEqual(int(chosen["id"]), 13)
        self.assertIsNone(
            choose_unseen_review_run(classified[:2], seen_ids=set()),
        )

    def test_dual_transport_does_not_false_complete_or_miscorrelate(self) -> None:
        current = "b" * 40
        skip_status = {
            "kind": CANDIDATE_PREPARE_SKIPPED,
            "run": {
                "id": 21,
                "event": "status",
                "status": "completed",
                "created_at": "2026-08-20T00:00:00Z",
            },
            "prepare": {
                "should_review": False,
                "pull_number": 19,
                "head_sha": current,
                "reason": "event sha is not the current pull head",
            },
        }
        review_check = {
            "kind": CANDIDATE_REVIEW_EXECUTED,
            "run": {
                "id": 22,
                "event": "check_run",
                "status": "completed",
                "created_at": "2026-08-20T00:01:00Z",
                "updated_at": "2026-08-20T00:02:00Z",
            },
            "prepare": {
                "should_review": True,
                "pull_number": 19,
                "head_sha": current,
                "reason": "ok",
            },
        }
        chosen = choose_unseen_review_run([skip_status, review_check], seen_ids=set())
        self.assertEqual(int(chosen["id"]), 22)
        self.assertEqual(str(chosen["event"]), "check_run")

        dual_review = [
            {
                "kind": CANDIDATE_REVIEW_EXECUTED,
                "run": {
                    "id": 31,
                    "event": "status",
                    "status": "completed",
                    "created_at": "2026-08-20T00:00:00Z",
                    "updated_at": "2026-08-20T00:01:00Z",
                },
            },
            {
                "kind": CANDIDATE_REVIEW_PENDING,
                "run": {
                    "id": 32,
                    "event": "check_run",
                    "status": "in_progress",
                    "created_at": "2026-08-20T00:00:30Z",
                },
            },
        ]
        self.assertIsNone(
            completed_review_run_for_terminal(
                dual_review,
                current_head=current,
                pr_head_sha=current,
                terminal="READY_FOR_HUMAN",
            )
        )
        self.assertEqual(
            int(choose_unseen_review_run(dual_review, seen_ids={31})["id"]),
            32,
        )

        dual_skip = [
            skip_status,
            {
                "kind": CANDIDATE_PREPARE_SKIPPED,
                "run": {
                    "id": 23,
                    "event": "check_run",
                    "status": "completed",
                    "created_at": "2026-08-20T00:01:00Z",
                },
            },
        ]
        self.assertIsNone(choose_unseen_review_run(dual_skip, seen_ids=set()))
        self.assertIsNone(
            completed_review_run_for_terminal(
                dual_skip,
                current_head=current,
                pr_head_sha=current,
                terminal="READY_FOR_HUMAN",
            )
        )
        both_complete = [
            {
                "kind": CANDIDATE_REVIEW_EXECUTED,
                "run": {
                    "id": 41,
                    "event": "status",
                    "status": "completed",
                    "created_at": "2026-08-20T00:00:00Z",
                    "updated_at": "2026-08-20T00:01:00Z",
                },
            },
            {
                "kind": CANDIDATE_REVIEW_EXECUTED,
                "run": {
                    "id": 42,
                    "event": "check_run",
                    "status": "completed",
                    "created_at": "2026-08-20T00:00:10Z",
                    "updated_at": "2026-08-20T00:02:00Z",
                },
            },
        ]
        latest = completed_review_run_for_terminal(
            both_complete,
            current_head=current,
            pr_head_sha=current,
            terminal="READY_FOR_HUMAN",
        )
        self.assertEqual(int(latest["id"]), 42)
        self.assertEqual(
            int(choose_unseen_review_run(both_complete, seen_ids=set())["id"]),
            41,
        )

    def test_timeout_evidence_row_includes_prepare_and_transport_fields(self) -> None:
        row = candidate_evidence_row(
            {
                "id": 55,
                "kind": CANDIDATE_PREPARE_SKIPPED,
                "event": "status",
                "html_url": "https://example.invalid/runs/55",
                "status": "completed",
                "conclusion": "success",
                "run_head_sha": "c" * 40,
                "prepare_log_error": False,
                "prepare": {
                    "should_review": False,
                    "reason": "event sha is not the current pull head",
                    "pull_number": 19,
                    "head_sha": "b" * 40,
                },
                "jobs": {"Prepare review": "success", "Review and repair": "skipped"},
            }
        )
        for key in (
            "id",
            "event",
            "run_conclusion",
            "prepare_job_conclusion",
            "prepare_output_available",
            "prepare_output_error",
            "should_review",
            "reason",
            "pull_number",
            "head_sha",
        ):
            self.assertIn(key, row)
        self.assertEqual(row["id"], 55)
        self.assertEqual(row["event"], "status")
        self.assertEqual(row["run_conclusion"], "success")
        self.assertEqual(row["prepare_job_conclusion"], "success")
        self.assertTrue(row["prepare_output_available"])
        self.assertFalse(row["prepare_output_error"])
        self.assertEqual(row["should_review"], False)
        self.assertEqual(row["reason"], "event sha is not the current pull head")
        self.assertEqual(row["pull_number"], 19)
        self.assertEqual(row["head_sha"], "b" * 40)
        timeout_source = inspect.getsource(GitHub.scenario_a_timeout_evidence)
        self.assertIn("candidate_evidence_row", timeout_source)
        self.assertIn("check_run_status_history", timeout_source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
