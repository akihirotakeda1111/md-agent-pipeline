from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from .harness.adapters import Phase6Driver, ServiceBundle
from .harness.fake_codex import CodexStep, ScriptedCodex
from .harness.fake_github import FakeGitHub
from .harness.git_repo import GitRepo, RecordingProcessRunner
from .harness.observations import ObservationLog


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--phase6-binding",
        default=os.environ.get("PHASE6_BINDING", "integration.invoke_phase6"),
        help="Import path exposing create_driver()",
    )
    parser.addoption(
        "--require-phase6-binding",
        action="store_true",
        default=False,
        help="Fail rather than skip if TODO_PRODUCTION_BINDING is unresolved",
    )
    parser.addoption(
        "--phase6-production-root",
        default=os.environ.get("PHASE6_PRODUCTION_ROOT", "."),
        help="Production repository root containing .github/workflows/agent-execute.yml",
    )


@pytest.fixture(scope="session")
def phase6_driver(request: pytest.FixtureRequest) -> Phase6Driver:
    module_name = request.config.getoption("--phase6-binding")
    try:
        module = importlib.import_module(module_name)
        driver = module.create_driver()
    except Exception as exc:
        if request.config.getoption("--require-phase6-binding"):
            pytest.fail(f"Production binding unavailable: {exc}", pytrace=False)
        pytest.skip(f"Production binding unavailable: {exc}")
    if not isinstance(driver, Phase6Driver):
        pytest.fail("create_driver() does not satisfy Phase6Driver", pytrace=False)
    return driver


@pytest.fixture(scope="session")
def production_root(request: pytest.FixtureRequest) -> Path:
    root = Path(request.config.getoption("--phase6-production-root")).resolve()
    workflow = root / ".github" / "workflows" / "agent-execute.yml"
    if not workflow.is_file():
        if request.config.getoption("--require-phase6-binding"):
            pytest.fail(f"Production workflow missing: {workflow}", pytrace=False)
        pytest.skip(f"Production workflow missing: {workflow}")
    return root


@pytest.fixture
def observations() -> ObservationLog:
    return ObservationLog()


@pytest.fixture
def git_repo(tmp_path: Path, observations: ObservationLog) -> GitRepo:
    return GitRepo.create(tmp_path, observations)


@pytest.fixture
def spec_path(git_repo: GitRepo) -> Path:
    source = Path(__file__).parent.parent / "fixtures" / "specs" / "01-multi-task.PASS.md"
    target = git_repo.root / "specs" / "tasks" / "phase6-integration.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    git_repo.git("add", str(target.relative_to(git_repo.root)))
    git_repo.git("commit", "-m", "add task spec")
    git_repo.git("push", "origin", "main")
    git_repo.observations.timeline.clear()
    git_repo.observations.processes.clear()
    return target


@pytest.fixture
def artifact_factory(tmp_path: Path, git_repo: GitRepo):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(exist_ok=True)

    def make(
        spec_path: Path,
        *,
        changes: dict[str, str] | None = None,
        report_overrides: dict[str, object] | None = None,
        patch_bytes: bytes | None = None,
    ):
        from agent.spec import bind_spec_identity, parse_spec
        from agent.state import ExecutionState, ExecutionStatus
        from agent.workunit import WorkUnitReport

        from .common import HAPPY_PATH_CHANGES
        from .harness.adapters import ArtifactBundle

        intended = changes or dict(HAPPY_PATH_CHANGES)
        actual_patch = patch_bytes if patch_bytes is not None else git_repo.make_patch(intended)
        patch_path = artifacts_dir / "changes.patch"
        patch_path.write_bytes(actual_patch)
        spec = parse_spec(spec_path)
        spec = bind_spec_identity(spec, repo_root=git_repo.root, spec_directory="specs/tasks")
        state = ExecutionState(
            schema_version=1,
            task_id=spec.id,
            state=ExecutionStatus.FINAL_VALIDATING,
            current_task="task-2" if len(spec.tasks) > 1 else "task-1",
            completed_tasks=tuple(task.id for task in spec.tasks),
            repair_attempts=0,
            review_attempts=0,
            last_validation=None,
            last_result="PASSED",
            branch=spec.target_branch,
            pull_request=None,
        )
        report = WorkUnitReport(
            outcome="FINAL_VERIFICATION_PASSED",
            spec_id=spec.id,
            spec_path=spec.source_path or "",
            spec_sha256=spec.spec_sha256,
            base_sha=git_repo.head,
            branch=spec.target_branch,
            state=state,
            completed_tasks=state.completed_tasks,
            changed_files=tuple(sorted(intended)),
            validation_results=tuple(task.validation for task in spec.tasks),
            repair_attempts=0,
            final_verification_passed=True,
            validation_passed=True,
            scope_allowed=True,
            message="ok",
            current_task=state.current_task,
            patch_file="changes.patch",
            patch_sha256=hashlib.sha256(actual_patch).hexdigest(),
        )
        payload = report.to_json_dict()
        payload.update(report_overrides or {})
        report_path = artifacts_dir / "report.json"
        report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        git_repo.observations.timeline.clear()
        git_repo.observations.processes.clear()
        return ArtifactBundle(report_path=report_path, patch_path=patch_path)

    return make


@pytest.fixture
def service_factory(git_repo: GitRepo, observations: ObservationLog):
    def make(
        *,
        codex_steps: list[CodexStep] | None = None,
        github_responses: dict[str, list[object]] | None = None,
        validation: list[bool] | None = None,
        final_validation: list[bool] | None = None,
    ) -> ServiceBundle:
        return ServiceBundle(
            codex=ScriptedCodex(codex_steps or [CodexStep()], observations),
            github=FakeGitHub(observations, github_responses),
            git_repo=git_repo,
            processes=RecordingProcessRunner(observations),
            observations=observations,
            validation_responses=validation or [True],
            final_validation_responses=final_validation or [True],
        )

    return make
