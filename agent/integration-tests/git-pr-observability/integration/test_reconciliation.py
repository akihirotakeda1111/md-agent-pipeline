from __future__ import annotations

import pytest

from .common import (
    assert_no_git_write,
    delivery_request,
    github_fixture,
    snapshot,
)
from .harness.adapters import require_status
from .harness.observations import event_names


def test_24_25_same_work_unit_open_pr_is_reused_without_new_delivery(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    existing = github_fixture("existing-same-work-unit.json")
    services = service_factory(github_responses={"list_pull_requests": [[existing]]})
    before = snapshot(git_repo)
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    require_status(result, "READY")
    assert result.pr_url == existing["html_url"]
    assert_no_git_write(before, git_repo, services)
    assert not services.github.calls("create_pull_request")
    assert git_repo.changed_paths() == []
    names = event_names(result.events or services.observations.events)
    assert "DELIVERY_VALIDATION_STARTED" not in names
    assert "DELIVERY_VALIDATION_PASSED" not in names


@pytest.mark.parametrize(
    ("case_id", "mutations"),
    [
        (
            "26",
            {
                "body": (
                    "<!-- md-agent-work-unit\n"
                    "spec_id: wrong\n"
                    "base_branch: main\n"
                    "target_branch: agent/phase6-integration\n"
                    "-->"
                )
            },
        ),
        ("27-head", {"head": {"ref": "agent/other"}}),
        ("27-base", {"base": {"ref": "release"}}),
        ("28", {"body": "no work-unit marker"}),
    ],
)
def test_26_28_unsafe_existing_pr_is_not_reused(
    case_id, mutations, phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    existing = github_fixture("existing-same-work-unit.json")
    existing.update(mutations)
    services = service_factory(github_responses={"list_pull_requests": [[existing]]})
    before = snapshot(git_repo)
    result = phase6_driver.deliver(
        delivery_request(spec_path, git_repo, artifact_factory(spec_path)), services
    )
    assert result.status.upper() == "ESCALATED", case_id
    assert_no_git_write(before, git_repo, services)
    assert not services.github.calls("create_pull_request")


def test_reuse_still_requires_report_and_patch_binding(
    phase6_driver, spec_path, git_repo, service_factory, artifact_factory
):
    existing = github_fixture("existing-same-work-unit.json")
    services = service_factory(github_responses={"list_pull_requests": [[existing]]})
    before = snapshot(git_repo)
    artifacts = artifact_factory(
        spec_path, report_overrides={"spec_id": "artifact-from-other-spec"}
    )
    result = phase6_driver.deliver(delivery_request(spec_path, git_repo, artifacts), services)
    assert result.status.upper() == "ESCALATED"
    assert_no_git_write(before, git_repo, services)
    assert not services.github.calls("create_pull_request")
