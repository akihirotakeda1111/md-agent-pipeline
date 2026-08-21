from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from .harness.adapters import Phase7Driver, ServiceBundle
from .harness.fake_classifier import ClassifierStep, ScriptedClassifier
from .harness.fake_codex import CodexStep, ScriptedCodex
from .harness.fake_github import FakeGitHub
from .harness.git_repo import GitRepo, RecordingProcessRunner
from .harness.observations import ObservationLog


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--phase7-binding", default=os.environ.get("PHASE7_BINDING", "integration.invoke_phase7")
    )
    parser.addoption("--require-phase7-binding", action="store_true", default=False)
    parser.addoption(
        "--phase7-production-root", default=os.environ.get("PHASE7_PRODUCTION_ROOT", ".")
    )


@pytest.fixture(scope="session")
def phase7_driver(request: pytest.FixtureRequest) -> Phase7Driver:
    try:
        module = importlib.import_module(request.config.getoption("--phase7-binding"))
        driver = module.create_driver()
    except Exception as exc:
        if request.config.getoption("--require-phase7-binding"):
            pytest.fail(f"Production binding unavailable: {exc}", pytrace=False)
        pytest.skip(f"Production binding unavailable: {exc}")
    if not isinstance(driver, Phase7Driver):
        pytest.fail("create_driver() does not satisfy Phase7Driver", pytrace=False)
    return driver


@pytest.fixture(scope="session")
def production_root(request: pytest.FixtureRequest) -> Path:
    root = Path(request.config.getoption("--phase7-production-root")).resolve()
    workflow = root / ".github" / "workflows" / "agent-review.yml"
    if not workflow.is_file():
        if request.config.getoption("--require-phase7-binding"):
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
    source = Path(__file__).parent.parent / "fixtures" / "specs" / "phase7-review.PASS.md"
    target = git_repo.root / "specs" / "tasks" / "phase7-integration.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    git_repo.git("add", str(target.relative_to(git_repo.root)))
    git_repo.git("commit", "-m", "add task spec")
    git_repo.git("push", "origin", "agent/phase7-integration")
    git_repo.observations.timeline.clear()
    git_repo.observations.processes.clear()
    return target


@pytest.fixture
def service_factory(git_repo: GitRepo, observations: ObservationLog):
    def make(
        *,
        github: dict[str, list[object]] | None = None,
        classifier: list[ClassifierStep] | None = None,
        codex: list[CodexStep] | None = None,
    ) -> ServiceBundle:
        return ServiceBundle(
            github=FakeGitHub(observations, github),
            classifier=ScriptedClassifier(classifier or [], observations),
            codex=ScriptedCodex(codex or [], observations),
            git_repo=git_repo,
            processes=RecordingProcessRunner(observations),
            observations=observations,
        )

    return make
