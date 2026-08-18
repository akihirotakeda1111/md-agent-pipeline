from __future__ import annotations

import inspect
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "yaml" not in sys.modules:
    sys.modules["yaml"] = types.SimpleNamespace(safe_load=lambda value: value)

import run as runner
from harness.assertions import (
    assert_commit_files,
    assert_no_new_delivery_events,
    assert_pr,
    assert_pr_files,
    assert_reuse,
    assert_successful_run,
)
from harness.git import GitRepository
from harness.github import GitHub
from harness.models import PullRequestEvidence, RunEvidence, WorkflowInfo, report_skeleton
from harness.workflow import choose_trigger, matches_filters, resolve_dispatch_inputs


class HarnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scenario = runner.make_scenario("owner/repo", "20260818-abcdef")

    def test_rendered_spec_is_isolated_and_has_no_template_markers(self) -> None:
        rendered = runner.render_spec(self.scenario)
        self.assertNotIn("{{", rendered)
        self.assertIn(self.scenario.task_id, rendered)
        self.assertIn(self.scenario.generated_file, rendered)
        self.assertIn(".agent/state/**", rendered)
        self.assertNotIn("terraform apply", rendered.lower())
        self.assertIn(f"== '{self.scenario.generated_content}\\n'", rendered)
        self.assertNotIn(f"== '{self.scenario.generated_content}\\\\n'", rendered)

    def test_scenario_names_are_unique_and_safely_prefixed(self) -> None:
        self.assertTrue(self.scenario.source_branch.startswith("e2e/phase6-"))
        self.assertTrue(self.scenario.target_branch.startswith("agent/phase6-e2e-"))
        self.assertTrue(self.scenario.task_spec.startswith("specs/tasks/_e2e-phase6-"))
        self.assertTrue(self.scenario.generated_file.startswith("app/e2e-phase6-"))
        self.assertEqual(self.scenario.base_branch, self.scenario.source_branch)

    def test_push_is_preferred_when_existing_filters_match(self) -> None:
        workflow = {"on": {"push": {"paths": ["specs/tasks/**/*.md"]}, "workflow_dispatch": {}}}
        trigger, definitions = choose_trigger(
            workflow, "auto", self.scenario.source_branch, self.scenario.task_spec
        )
        self.assertEqual(trigger, "push")
        self.assertEqual(definitions, {})
        trigger, _ = choose_trigger(
            workflow, "push", self.scenario.source_branch, self.scenario.task_spec
        )
        self.assertEqual(trigger, "push")

    def test_dispatch_inputs_use_only_existing_interface(self) -> None:
        definitions = {"spec_path": {"required": True}, "mode": {"default": "normal"}}
        values = resolve_dispatch_inputs(
            definitions, {}, spec_path=self.scenario.task_spec, task_id=self.scenario.task_id
        )
        self.assertEqual(values, {"spec_path": self.scenario.task_spec, "mode": "normal"})
        with self.assertRaises(ValueError):
            resolve_dispatch_inputs(
                definitions,
                {"e2e_only_input": "x"},
                spec_path=self.scenario.task_spec,
                task_id=self.scenario.task_id,
            )

    def test_github_style_filters_handle_include_exclude_order(self) -> None:
        self.assertTrue(matches_filters("e2e/phase6-id", ["e2e/**", "!e2e/private/**"], None))
        self.assertFalse(matches_filters("e2e/private/id", ["e2e/**", "!e2e/private/**"], None))
        self.assertTrue(
            matches_filters("specs/tasks/_e2e-phase6-id.md", ["specs/tasks/**/*.md"], None)
        )
        self.assertTrue(
            matches_filters("specs/tasks/nested/demo.md", ["specs/tasks/**/*.md"], None)
        )

    def test_run_pr_commit_and_reuse_assertions(self) -> None:
        workflow = WorkflowInfo(
            1,
            "Production Agent",
            ".github/workflows/agent-execute.yml",
            "active",
            "push",
            {},
        )
        run1 = RunEvidence(
            10,
            1,
            "https://example.invalid/runs/10",
            "a" * 40,
            self.scenario.source_branch,
            "push",
            "success",
            {"execute": "success", "deliver": "success"},
        )
        assert_successful_run(run1, workflow, self.scenario, "a" * 40, 1)
        body = "\n".join(
            [
                "<!-- agent-work-unit: " + self.scenario.task_id + " -->",
                "Task Spec",
                "Objective",
                "Completed Tasks",
                "Changed Files",
                "Validation Results",
                "Final Verification",
                "Repair Attempts",
            ]
        )
        pr1 = PullRequestEvidence(
            4,
            "https://example.invalid/pull/4",
            self.scenario.target_branch,
            self.scenario.base_branch,
            "b" * 40,
            body,
            ("agent:ready",),
        )
        assert_pr(pr1, self.scenario)
        assert_commit_files([self.scenario.generated_file], self.scenario)
        assert_pr_files([self.scenario.generated_file], self.scenario)
        assert_reuse(pr1, pr1, "b" * 40)
        assert_no_new_delivery_events(["SPEC_VALIDATED", "WORKFLOW_COMPLETED"])

    def test_report_has_required_machine_readable_fields(self) -> None:
        report = report_skeleton(self.scenario)
        for key in (
            "scenario_id",
            "source_branch",
            "task_spec",
            "task_id",
            "target_branch",
            "base_branch",
            "run1",
            "run2",
            "pr_count",
            "cleanup",
            "result",
        ):
            self.assertIn(key, report)
        self.assertEqual(report["base_branch"], self.scenario.source_branch)
        with tempfile.TemporaryDirectory() as temp:
            path = runner.save_report(Path(temp), report)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["scenario_id"], self.scenario.scenario_id)

    def test_harness_git_has_no_history_rewrite_commands(self) -> None:
        source = inspect.getsource(GitRepository).lower()
        for forbidden in ("--force", "--amend", "rebase", "merge", "reset --hard"):
            self.assertNotIn(forbidden, source)

    def test_harness_does_not_read_codex_secret(self) -> None:
        source = (
            inspect.getsource(runner) + inspect.getsource(GitHub) + inspect.getsource(GitRepository)
        )
        self.assertNotIn("CODEX_API_KEY", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
