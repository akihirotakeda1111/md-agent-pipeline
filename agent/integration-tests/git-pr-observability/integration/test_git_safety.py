from __future__ import annotations

from .common import (
    NEW_PR_GITHUB,
    TARGET_BRANCH,
    assert_linear_feature_commit,
    assert_no_git_write,
    delivery_request,
    snapshot,
)


def test_19_dirty_worktree_or_index_fails_safely(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    artifacts = artifact_factory(spec_path)
    git_repo.write("app/dirty.txt", "dirty\n")
    git_repo.git("add", "app/dirty.txt")
    before = snapshot(git_repo)
    services = service_factory()
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert result.status.upper() in {"FAILED", "ESCALATED"}
    assert result.reason and ("DIRTY" in result.reason.upper() or "CLEAN" in result.reason.upper())
    assert_no_git_write(before, git_repo, services)


def test_20_23_no_force_amend_rebase_merge_or_history_rewrite_on_success(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    services = service_factory(github_responses=NEW_PR_GITHUB)
    before = snapshot(git_repo)
    phase6_driver.deliver(delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services)
    assert_linear_feature_commit(before, git_repo)


def test_20_23_no_history_rewrite_on_base_mismatch(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    artifacts = artifact_factory(spec_path)
    git_repo.git("checkout", "-b", TARGET_BRANCH)
    git_repo.write("app/concurrent.txt", "advance\n")
    git_repo.git("add", "app/concurrent.txt")
    git_repo.git("commit", "-m", "advance")
    before = snapshot(git_repo)
    services = service_factory()
    phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert git_repo.head == before.head
    assert git_repo.commit_count == before.commits
    assert git_repo.remote_ref(TARGET_BRANCH) == before.remote_target
    parent = git_repo.git("rev-parse", "HEAD^").stdout.strip()
    assert parent != ""
