from __future__ import annotations

import hashlib
import json

import pytest

from .common import (
    NEW_PR_GITHUB,
    TARGET_BRANCH,
    assert_linear_feature_commit,
    assert_no_git_write,
    assert_reason,
    delivery_request,
    snapshot,
)
from .harness.adapters import require_status
from .harness.observations import event_names


def test_06_happy_path_commits_pushes_and_creates_pr(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses=NEW_PR_GITHUB)
    artifacts = artifact_factory(spec_path)
    before = snapshot(git_repo)
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    require_status(result, "READY")
    assert_linear_feature_commit(before, git_repo)
    assert len(services.github.calls("create_pull_request")) == 1


def test_07_commit_occurs_after_final_verification(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses=NEW_PR_GITHUB)
    before = snapshot(git_repo)
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    require_status(result, "READY")
    names = event_names(result.events or services.observations.events)
    assert names.index("DELIVERY_VALIDATION_PASSED") < names.index("PR_CREATED")
    assert git_repo.commit_count == before.commits + 1


@pytest.mark.parametrize(
    ("case_id", "changes"),
    [
        ("08", {"docs/outside.txt": "outside\n"}),
        ("09", {"app/forbidden/value.txt": "forbidden\n"}),
        ("10", {".agent/state/phase6-integration.json": "{}\n"}),
    ],
)
def test_08_10_scope_failures_do_not_git_write(
    case_id, changes, phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    artifacts = artifact_factory(spec_path, changes=changes)
    before = snapshot(git_repo)
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert result.status.upper() in {"SCOPE_VIOLATION", "ESCALATED"}, case_id
    assert_no_git_write(before, git_repo, services)


@pytest.mark.parametrize(
    ("case_id", "override", "reason"),
    [
        ("11", {"spec_id": "different"}, "REPORT_SPEC_MISMATCH"),
        ("12", {"branch": "agent/different"}, "REPORT_BRANCH_MISMATCH"),
        ("13", {"spec_path": "specs/tasks/different.md"}, "REPORT_SPEC_MISMATCH"),
        ("15", {"base_sha": ""}, "BASE_SHA_MISSING"),
    ],
)
def test_11_15_report_context_mismatch_escalates_without_write(
    case_id, override, reason, phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory()
    artifacts = artifact_factory(spec_path, report_overrides=override)
    before = snapshot(git_repo)
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert_reason(result, reason)
    assert_no_git_write(before, git_repo, services)
    assert git_repo.changed_paths() == []


def test_14_patch_digest_mismatch_escalates_without_write(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory()
    artifacts = artifact_factory(
        spec_path, report_overrides={"patch_sha256": hashlib.sha256(b"other").hexdigest()}
    )
    before = snapshot(git_repo)
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert_reason(result, "PATCH_DIGEST_MISMATCH")
    assert_no_git_write(before, git_repo, services)
    assert git_repo.changed_paths() == []


def test_16_base_sha_mismatch_escalates_without_rebase_or_merge(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    artifacts = artifact_factory(spec_path)
    report = json.loads(artifacts.report_path.read_text(encoding="utf-8"))
    git_repo.git("checkout", "-b", TARGET_BRANCH)
    git_repo.write("app/concurrent.txt", "new head\n")
    git_repo.git("add", "app/concurrent.txt")
    git_repo.git("commit", "-m", "move feature branch")
    before = snapshot(git_repo)
    services = service_factory()
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert report["base_sha"] != git_repo.head
    assert_reason(result, "BASE_SHA_MISMATCH")
    assert_no_git_write(before, git_repo, services)


def test_17_patch_manifest_mismatch_does_not_commit(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    artifacts = artifact_factory(spec_path, report_overrides={"changed_files": ["app/declared-only.txt"]})
    before = snapshot(git_repo)
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert result.reason and "PATCH_MANIFEST_MISMATCH" in result.reason.upper()
    assert_no_git_write(before, git_repo, services)


def test_18_deliver_final_verification_failure_does_not_git_write(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses={"list_pull_requests": [[]]})
    artifacts = artifact_factory(spec_path, changes={"app/result.txt": "phase6\n"})
    before = snapshot(git_repo)
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert result.status.upper() in {"FAILED", "ESCALATED"}
    assert_no_git_write(before, git_repo, services)
