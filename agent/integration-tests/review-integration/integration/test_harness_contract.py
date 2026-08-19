from __future__ import annotations

import json

from .harness.fake_classifier import ScriptedClassifier, classification
from .harness.fake_codex import CodexStep, ScriptedCodex
from .harness.fake_github import FakeGitHub
from .harness.git_repo import GitRepo
from .harness.observations import ObservationLog


def test_real_git_repository_and_bare_remote(tmp_path):
    observations = ObservationLog()
    repo = GitRepo.create(tmp_path, observations)
    before = repo.head
    repo.write("app/review.txt", "repaired\n")
    repo.git("add", "app/review.txt")
    repo.git("commit", "-m", "review fix")
    repo.git("push", "origin", "agent/phase7-integration")
    assert repo.head != before
    assert repo.remote_ref() == repo.head


def test_fakes_only_record_scripted_io(tmp_path):
    observations = ObservationLog()
    classifier = ScriptedClassifier([classification("ACTIONABLE")], observations)
    raw = classifier.classify(
        payload={"body": "x"}, schema={"type": "object"}, env={"REVIEW_CLASSIFIER_API_KEY": "x"}
    )
    assert json.loads(raw)["classification"] == "ACTIONABLE"
    codex = ScriptedCodex([CodexStep({"app/value.txt": "value\n"})], observations)
    codex.invoke(workspace=tmp_path, prompt="repair", env={"CODEX_API_KEY": "x"})
    github = FakeGitHub(observations, {"list_review_feedback": [[{"id": 1}]]})
    assert github.list_review_feedback(pr_number=42) == [{"id": 1}]
    assert github.calls("list_review_feedback") == [{"pr_number": 42}]


def test_observations_are_valid_json_lines(tmp_path):
    observations = ObservationLog(events=[{"event": "REVIEW_RECEIVED", "pr_number": 42}])
    target = tmp_path / "events.jsonl"
    observations.write_jsonl(target)
    assert [
        json.loads(line)["event"] for line in target.read_text(encoding="utf-8").splitlines()
    ] == ["REVIEW_RECEIVED"]
