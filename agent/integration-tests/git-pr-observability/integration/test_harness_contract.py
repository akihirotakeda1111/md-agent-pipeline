from __future__ import annotations

import hashlib
import json

from .harness.fake_codex import CodexStep, ScriptedCodex
from .harness.fake_github import FakeGitHub
from .harness.git_repo import GitRepo
from .harness.observations import ObservationLog


def test_harness_git_repo_uses_real_commits_and_patch_bytes(tmp_path):
    observations = ObservationLog()
    repo = GitRepo.create(tmp_path, observations)
    base = repo.head
    patch = repo.make_patch({"app/result.txt": "result\n"})
    assert patch.startswith(b"diff --git")
    assert repo.head == base
    assert repo.changed_paths() == []
    assert len(hashlib.sha256(patch).hexdigest()) == 64


def test_harness_fakes_only_record_scripted_io(tmp_path):
    observations = ObservationLog()
    codex = ScriptedCodex([CodexStep({"app/value.txt": "value\n"})], observations)
    codex.invoke(workspace=tmp_path, env={"CODEX_API_KEY": "x"})
    github = FakeGitHub(observations, {"list_pull_requests": [[{"number": 1}]]})
    assert github.list_pull_requests(head="branch") == [{"number": 1}]
    assert (tmp_path / "app" / "value.txt").read_text(encoding="utf-8") == "value\n"
    assert github.calls("list_pull_requests") == [{"head": "branch"}]


def test_observations_are_valid_json_lines(tmp_path):
    observations = ObservationLog(events=[{"event": "SPEC_DISCOVERED", "task_id": "x"}])
    target = tmp_path / "events.jsonl"
    observations.write_jsonl(target)
    assert [json.loads(line)["event"] for line in target.read_text(encoding="utf-8").splitlines()] == [
        "SPEC_DISCOVERED"
    ]
