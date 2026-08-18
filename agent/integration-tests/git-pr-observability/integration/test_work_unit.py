from __future__ import annotations

from .harness.adapters import WorkUnitRequest, require_status
from .harness.fake_codex import CodexStep
from .harness.observations import event_names


def request(spec_path, git_repo):
    return WorkUnitRequest(
        spec_path=spec_path,
        repo_root=git_repo.root,
        persist_state=False,
        environment={"CODEX_API_KEY": "codex-test-key"},
    )


def test_01_multiple_tasks_run_in_order(phase6_driver, spec_path, git_repo, service_factory):
    services = service_factory(
        codex_steps=[CodexStep({"app/task-1.txt": "one\n"}), CodexStep({"app/task-2.txt": "two\n"})]
    )
    result = phase6_driver.run_work_unit(request(spec_path, git_repo), services)
    require_status(result, "PASSED")
    assert len(services.codex.invocations) == 2
    assert result.completed_tasks == ["task-1", "task-2"]
    assert (git_repo.root / "app" / "task-1.txt").read_text(encoding="utf-8") == "one\n"
    assert (git_repo.root / "app" / "task-2.txt").read_text(encoding="utf-8") == "two\n"


def test_02_memory_state_flows_between_tasks(phase6_driver, spec_path, git_repo, service_factory):
    services = service_factory(
        codex_steps=[CodexStep({"app/task-1.txt": "one\n"}), CodexStep({"app/task-2.txt": "two\n"})]
    )
    result = phase6_driver.run_work_unit(request(spec_path, git_repo), services)
    require_status(result, "PASSED")
    names = event_names(result.events or services.observations.events)
    first_done = names.index("TASK_COMPLETED")
    second_start = names.index("TASK_STARTED", first_done + 1)
    assert first_done < second_start
    assert not (git_repo.root / ".agent" / "state").exists()


def test_03_missing_ephemeral_state_restarts_from_task_one(
    phase6_driver, spec_path, git_repo, service_factory
):
    state = git_repo.root / ".agent" / "state" / "phase6-integration.json"
    assert not state.exists()
    services = service_factory(
        codex_steps=[CodexStep({"app/task-1.txt": "one\n"}), CodexStep({"app/task-2.txt": "two\n"})]
    )
    result = phase6_driver.run_work_unit(request(spec_path, git_repo), services)
    require_status(result, "PASSED")
    assert len(services.codex.invocations) == 2
    assert (git_repo.root / "app" / "task-1.txt").is_file()
    assert not state.exists()


def test_04_validation_failure_repairs_within_bound(
    phase6_driver, spec_path, git_repo, service_factory
):
    services = service_factory(
        codex_steps=[
            CodexStep(),
            CodexStep({"app/task-1.txt": "repaired\n"}),
            CodexStep({"app/task-2.txt": "two\n"}),
        ]
    )
    result = phase6_driver.run_work_unit(request(spec_path, git_repo), services)
    require_status(result, "PASSED")
    assert len(services.codex.invocations) == 3
    assert (git_repo.root / "app" / "task-1.txt").read_text(encoding="utf-8") == "repaired\n"
    names = event_names(result.events or services.observations.events)
    assert "VALIDATION_FAILED" in names
    assert "REPAIR_STARTED" in names


def test_05_scope_violation_stops_work_unit(phase6_driver, spec_path, git_repo, service_factory):
    services = service_factory(codex_steps=[CodexStep({"docs/outside.txt": "forbidden\n"})])
    result = phase6_driver.run_work_unit(request(spec_path, git_repo), services)
    assert result.status.upper() in {"SCOPE_VIOLATION", "ESCALATED"}
    assert len(services.codex.invocations) == 1
    assert not (git_repo.root / "app" / "task-2.txt").exists()
