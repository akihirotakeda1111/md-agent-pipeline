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
    assert_execute_run,
    assert_linear_head_change,
    assert_non_coderabbit_short_circuit,
    assert_pr,
    assert_pr_scope,
    assert_review_run,
    assert_tracking_current_head,
    terminal_state,
)
from harness.coderabbit_terminal import KIND_COMPLETED, KIND_NONE, KIND_SKIPPED, resolve_coderabbit_terminal
from harness.git import GitRepository
from harness.github import GitHub
from harness.models import (
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
                "issue_comment": {"types": ["created"]},
                "check_run": {"types": ["completed"]},
                "status": None,
            },
            "concurrency": {"group": "agent-review-pr", "cancel-in-progress": False},
            "jobs": {},
        }
        self.assertEqual(
            assert_review_workflow_contract(workflow),
            ("issue_comment", "check_run", "status"),
        )

    def test_review_workflow_contract_requires_terminal_wakeups(self) -> None:
        workflow = {
            "on": {"issue_comment": {"types": ["created"]}},
            "concurrency": {"group": "agent-review-pr", "cancel-in-progress": False},
            "jobs": {},
        }
        with self.assertRaises(ProductionBug):
            assert_review_workflow_contract(workflow)

    def test_harness_terminal_mapper_ignores_old_head_and_maps_skipped(self) -> None:
        completed = resolve_coderabbit_terminal(
            [
                {
                    "head_sha": "abc",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-08-20T00:00:00Z",
                    "app": {"slug": "coderabbitai"},
                }
            ],
            [],
            head_sha="abc",
            actor="coderabbitai[bot]",
            check_app_slug="coderabbitai",
            status_context="CodeRabbit",
        )
        skipped = resolve_coderabbit_terminal(
            [
                {
                    "head_sha": "abc",
                    "status": "completed",
                    "conclusion": "skipped",
                    "completed_at": "2026-08-20T00:00:00Z",
                    "app": {"slug": "coderabbitai"},
                }
            ],
            [],
            head_sha="abc",
            actor="coderabbitai[bot]",
            check_app_slug="coderabbitai",
            status_context="CodeRabbit",
        )
        stale = resolve_coderabbit_terminal(
            [
                {
                    "head_sha": "old",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"slug": "coderabbitai"},
                }
            ],
            [],
            head_sha="new",
            actor="coderabbitai[bot]",
            check_app_slug="coderabbitai",
            status_context="CodeRabbit",
        )
        self.assertEqual(completed["kind"], KIND_COMPLETED)
        self.assertEqual(skipped["kind"], KIND_SKIPPED)
        self.assertEqual(stale["kind"], KIND_NONE)

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
        review = RunEvidence(
            11,
            1,
            "https://example.invalid/runs/11",
            "b" * 40,
            self.scenario.target_branch,
            "issue_comment",
            actor,
            "completed",
            "success",
            {"Prepare review": "success", "Review and repair": "success"},
            ("REVIEW_RECEIVED", "REVIEW_CLASSIFIED"),
        )
        assert_review_run(
            review, configured_actor=actor, supported_events=("issue_comment", "check_run")
        )
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
        assert_review_run(
            check_run, configured_actor=actor, supported_events=("issue_comment", "check_run")
        )
        negative = RunEvidence(
            12,
            1,
            "https://example.invalid/runs/12",
            "b" * 40,
            "main",
            "issue_comment",
            "human",
            "completed",
            "success",
            {"Prepare review": "success", "Review and repair": "skipped"},
            (),
        )
        assert_non_coderabbit_short_circuit(negative, actor)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
