"""RF-03: bind Work Unit identity with canonical spec_path and spec_sha256."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from agent.errors import AgentError
from agent.gitwrite import head_sha
from agent.pr import (
    authorize_work_unit_reuse,
    build_work_unit_marker,
    is_same_work_unit_pull,
    parse_work_unit_marker,
)
from agent.review_loop import load_review_track, run_review
from agent.review_prepare import prepare_review
from agent.review_track import (
    REVIEW_TRACK_SCHEMA_VERSION,
    empty_review_track,
    parse_review_track,
    render_review_track,
    with_processed,
)
from agent.spec import (
    bind_spec_identity,
    canonicalize_spec_path,
    is_canonical_spec_path,
    parse_spec,
    spec_source_sha256,
)
from agent.state import ExecutionStatus, new_execution_state
from agent.tests.test_phase6 import DELIVER_SPEC, _bound_example_spec, _FakeGitHub, _report
from agent.tests.test_phase7 import (
    FakeGithub,
    _check_run_event,
    _git,
    _pull,
    _repo,
    _run,
    _spec,
    _spec_files,
)
from agent.workunit import run_work_unit

SPEC_DIR = "specs/tasks"


def _bind(spec, repo: Path):
    return bind_spec_identity(spec, repo_root=repo, spec_directory=SPEC_DIR)


def _work_unit_pull(spec):
    from agent.pr import build_pr_body

    return {
        "number": 7,
        "html_url": "https://example.test/pull/7",
        "head": {"ref": spec.target_branch},
        "base": {"ref": spec.base_branch},
        "body": build_pr_body(
            spec,
            completed_tasks=["task-1"],
            changed_files=["src/app.py"],
            validation_results=[],
            final_verification="PASSED",
            repair_attempts=0,
        ),
    }


def test_canonical_spec_path_is_posix_and_repo_relative(tmp_path: Path) -> None:
    dest = tmp_path / "specs" / "tasks" / "example-task.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("# unused\n", encoding="utf-8")
    canonical = canonicalize_spec_path(
        str(dest),
        repo_root=tmp_path,
        spec_directory=SPEC_DIR,
    )
    assert canonical == "specs/tasks/example-task.md"
    assert is_canonical_spec_path(canonical, spec_directory=SPEC_DIR)


def test_windows_path_is_converted_to_posix_repo_relative() -> None:
    canonical = canonicalize_spec_path(
        r"C:\repo\specs\tasks\example-task.md",
        repo_root=r"C:\repo",
        spec_directory=SPEC_DIR,
    )
    assert canonical == "specs/tasks/example-task.md"


def test_path_outside_repository_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "escape.md"
    with pytest.raises(AgentError) as exc_info:
        canonicalize_spec_path(
            str(outside),
            repo_root=tmp_path,
            spec_directory=SPEC_DIR,
        )
    assert exc_info.value.code == "SPEC_PATH_ESCAPE"


def test_canonicalization_collapses_dot_segments_and_rejects_them_as_identity() -> None:
    canonical = canonicalize_spec_path(
        "specs/tasks/../tasks/example-task.md",
        repo_root="/repo",
        spec_directory=SPEC_DIR,
    )
    assert canonical == "specs/tasks/example-task.md"
    assert not is_canonical_spec_path(
        "specs/tasks/../tasks/example-task.md",
        spec_directory=SPEC_DIR,
    )


def test_newline_only_difference_shares_digest() -> None:
    lf = "line\nline\n"
    crlf = "line\r\nline\r\n"
    cr = "line\rline\r"
    assert spec_source_sha256(lf) == spec_source_sha256(crlf)
    assert spec_source_sha256(lf) == spec_source_sha256(cr)


def test_work_unit_report_records_canonical_path_and_digest(tmp_path: Path) -> None:
    from agent.codex_runner import ProcessResult

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rf03@example.com")
    _git(repo, "config", "user.name", "RF03")
    spec_path = repo / "specs" / "tasks" / "deliver-demo.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", "specs/tasks/deliver-demo.md")
    _git(repo, "commit", "-m", "init")

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("ok\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    import os
    import sys

    env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
    report = run_work_unit(
        spec_path,
        repo_root=repo,
        report_dir=tmp_path / "out",
        persist_state=False,
        env=env,
        executor=executor,
    )
    assert report.spec_path == "specs/tasks/deliver-demo.md"
    assert report.spec_sha256 == spec_source_sha256(DELIVER_SPEC)
    assert is_canonical_spec_path(report.spec_path, spec_directory=SPEC_DIR)


def test_delivery_digest_match_continues(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(report_dir, _report(spec=spec, patch_sha256=file_sha256(patch)))
    github = _FakeGitHub(pulls=[_work_unit_pull(spec)])
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "PR_CREATED"
    assert github.created_pulls == 0


def test_delivery_digest_mismatch_stops_before_reconcile(tmp_path: Path, monkeypatch) -> None:
    from agent import delivery
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(
        report_dir,
        _report(spec=spec, spec_sha256="0" * 64, patch_sha256=file_sha256(patch)),
    )
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("reconcile_open_pull must not run")

    monkeypatch.setattr(delivery, "reconcile_open_pull", boom)
    github = _FakeGitHub()
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "SPEC_IDENTITY_MISMATCH"
    assert called["n"] == 0
    assert github.created_pulls == 0


def test_delivery_path_mismatch_stops_before_reconcile(tmp_path: Path, monkeypatch) -> None:
    from agent import delivery
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(
        report_dir,
        _report(spec=spec, spec_path="specs/tasks/other.md", patch_sha256=file_sha256(patch)),
    )
    monkeypatch.setattr(
        delivery,
        "reconcile_open_pull",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no reconcile")),
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=_FakeGitHub(),  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.code == "SPEC_IDENTITY_MISMATCH"


def test_delivery_missing_spec_sha256_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from agent import delivery
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(
        report_dir,
        _report(spec=spec, spec_sha256="", patch_sha256=file_sha256(patch)),
    )
    monkeypatch.setattr(
        delivery,
        "reconcile_open_pull",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no reconcile")),
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=_FakeGitHub(),  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "SPEC_IDENTITY_MISMATCH"


def test_delivery_noncanonical_report_path_fails_closed(tmp_path: Path, monkeypatch) -> None:
    from agent import delivery
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(
        report_dir,
        _report(
            spec=spec,
            spec_path="specs/tasks/../tasks/example-task.md",
            patch_sha256=file_sha256(patch),
        ),
    )
    monkeypatch.setattr(
        delivery,
        "reconcile_open_pull",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no reconcile")),
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=_FakeGitHub(),  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.code == "SPEC_IDENTITY_MISMATCH"


def test_delivery_runs_patch_and_spec_digest_validation(tmp_path: Path, monkeypatch) -> None:
    from agent import delivery
    from agent.delivery import run_delivery
    from agent.workunit import write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    (report_dir / "changes.patch").write_text("diff\n", encoding="utf-8")
    write_work_unit_report(report_dir, _report(spec=spec, patch_sha256="0" * 64))
    monkeypatch.setattr(
        delivery,
        "reconcile_open_pull",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no reconcile")),
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=_FakeGitHub(),  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.code == "PATCH_DIGEST_MISMATCH"


def test_old_marker_is_still_a_candidate_but_reuse_is_denied() -> None:
    spec = _bound_example_spec()
    old_body = "\n".join(
        [
            "<!-- md-agent-work-unit",
            f"spec_id: {spec.id}",
            f"base_branch: {spec.base_branch}",
            f"target_branch: {spec.target_branch}",
            "-->",
        ]
    )
    marker = parse_work_unit_marker(old_body)
    assert marker is not None
    assert "spec_path" not in marker
    assert "spec_sha256" not in marker
    pull = {
        "body": old_body,
        "head": {"ref": spec.target_branch},
        "base": {"ref": spec.base_branch},
    }
    assert is_same_work_unit_pull(spec, pull) is True
    with pytest.raises(AgentError) as exc_info:
        authorize_work_unit_reuse(spec, pull)
    assert exc_info.value.code == "SPEC_IDENTITY_MISMATCH"


def test_marker_digest_mismatch_refuses_reuse_without_new_pr(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(report_dir, _report(spec=spec, patch_sha256=file_sha256(patch)))
    foreign = build_work_unit_marker(spec).replace(spec.spec_sha256, "b" * 64)
    github = _FakeGitHub(
        pulls=[
            {
                "number": 7,
                "html_url": "https://example.test/pull/7",
                "head": {"ref": spec.target_branch},
                "base": {"ref": spec.base_branch},
                "body": foreign,
            }
        ]
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.code == "SPEC_IDENTITY_MISMATCH"
    assert github.created_pulls == 0


def test_marker_path_mismatch_refuses_reuse(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.workunit import file_sha256, write_work_unit_report

    spec = _bound_example_spec(tmp_path)
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    patch = report_dir / "changes.patch"
    patch.write_text("", encoding="utf-8")
    write_work_unit_report(report_dir, _report(spec=spec, patch_sha256=file_sha256(patch)))
    foreign = build_work_unit_marker(spec).replace(spec.source_path or "", "specs/tasks/moved.md")
    github = _FakeGitHub(
        pulls=[
            {
                "number": 7,
                "html_url": "https://example.test/pull/7",
                "head": {"ref": spec.target_branch},
                "base": {"ref": spec.base_branch},
                "body": foreign,
            }
        ]
    )
    result = run_delivery(
        spec,
        repo_root=tmp_path,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.code == "SPEC_IDENTITY_MISMATCH"
    assert github.created_pulls == 0


def test_race_reconciliation_rechecks_digest(tmp_path: Path) -> None:
    from agent.delivery import run_delivery
    from agent.gitwrite import export_patch
    from agent.workunit import file_sha256, write_work_unit_report

    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rf03@example.com")
    _git(repo, "config", "user.name", "RF03")
    spec_path = repo / "specs" / "tasks" / "deliver-demo.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text(DELIVER_SPEC, encoding="utf-8")
    _git(repo, "add", "specs/tasks/deliver-demo.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "remote", "add", "origin", str(origin))
    base = head_sha(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    report_dir = tmp_path / "report"
    export_patch(repo, base, report_dir / "changes.patch")
    spec = _bind(parse_spec(spec_path), repo)
    report = _report(
        spec=spec,
        base_sha=base,
        branch=spec.target_branch,
        changed_files=("src/app.py",),
        patch_sha256=file_sha256(report_dir / "changes.patch"),
        state=replace(new_execution_state(spec), state=ExecutionStatus.FINAL_VALIDATING),
    )
    write_work_unit_report(report_dir, report)
    (repo / "src" / "app.py").unlink()

    class RacingGithub(_FakeGitHub):
        def create_pull(self, *, title: str, head: str, base: str, body: str):
            self.pulls = [
                {
                    "number": 9,
                    "html_url": "https://example.test/pull/9",
                    "head": {"ref": spec.target_branch},
                    "base": {"ref": spec.base_branch},
                    "body": build_work_unit_marker(spec).replace(spec.spec_sha256, "c" * 64),
                }
            ]
            raise AgentError.environment_failure(
                "pull already exists",
                code="GITHUB_API_VALIDATION",
            )

    github = RacingGithub()
    result = run_delivery(
        spec,
        repo_root=repo,
        report_dir=report_dir,
        github=github,  # type: ignore[arg-type]
        summary_path=tmp_path / "summary.md",
    )
    assert result.code == "SPEC_IDENTITY_MISMATCH"
    assert github.created_pulls == 0


def test_review_track_round_trip_is_schema_version_2() -> None:
    spec = _bound_example_spec()
    track = empty_review_track(spec)
    assert track.schema_version == REVIEW_TRACK_SCHEMA_VERSION == 2
    parsed = parse_review_track(render_review_track(track))
    assert parsed is not None
    assert parsed.spec_path == spec.source_path
    assert parsed.spec_sha256 == spec.spec_sha256
    assert parsed.matches_work_unit(spec)


def test_review_track_version_1_is_unsafe() -> None:
    body = "\n".join(
        [
            "<!-- md-agent-review-state",
            "schema_version: 1",
            "spec_id: example-task",
            "base_branch: main",
            "target_branch: feature/example",
            "head_sha: abc",
            "review_attempts: 4",
            "processed:",
            "- keep-me",
            "-->",
        ]
    )
    assert parse_review_track(body) is None


def test_review_track_version_2_digest_mismatch_is_unsafe() -> None:
    spec = _bound_example_spec()
    track = with_processed(empty_review_track(spec), (), increment=False)
    body = render_review_track(track).replace(spec.spec_sha256, "d" * 64)
    parsed = parse_review_track(body)
    assert parsed is not None
    assert parsed.matches_work_unit(spec) is False


def test_review_track_version_2_path_mismatch_is_unsafe() -> None:
    spec = _bound_example_spec()
    track = empty_review_track(spec)
    body = render_review_track(track).replace(spec.source_path or "", "specs/tasks/moved.md")
    parsed = parse_review_track(body)
    assert parsed is not None
    assert parsed.matches_work_unit(spec) is False


def test_run_review_rejects_changed_acceptance_criteria(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    original = (repo / "specs" / "tasks" / "review-demo.md").read_text(encoding="utf-8")
    changed = original.replace("- File exists.", "- File exists and is renamed.")
    (repo / "specs" / "tasks" / "review-demo.md").write_text(changed, encoding="utf-8")
    result = run_review(
        repo_root=repo,
        pull_number=7,
        head_sha_expected=head_sha(repo),
        spec_path="specs/tasks/review-demo.md",
        github=FakeGithub(_pull(repo, spec)).client(),
        env={"CODEX_API_KEY": "codex-secret", "REVIEW_CLASSIFIER_API_KEY": "review-secret"},
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "SPEC_IDENTITY_MISMATCH"


def test_run_review_without_prepare_rejects_identity_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    pull = _pull(repo, spec)
    pull["body"] = build_work_unit_marker(spec).replace(spec.spec_sha256, "e" * 64)
    result = run_review(
        repo_root=repo,
        pull_number=7,
        head_sha_expected=head_sha(repo),
        spec_path="specs/tasks/review-demo.md",
        github=FakeGithub(pull).client(),
        env={"CODEX_API_KEY": "x", "REVIEW_CLASSIFIER_API_KEY": "y"},
    )
    assert result.code == "SPEC_IDENTITY_MISMATCH"


def test_review_rejects_spec_path_move(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    original = repo / "specs" / "tasks" / "review-demo.md"
    moved = repo / "specs" / "tasks" / "moved.md"
    moved.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
    original.unlink()
    result = run_review(
        repo_root=repo,
        pull_number=7,
        head_sha_expected=head_sha(repo),
        spec_path="specs/tasks/review-demo.md",
        github=FakeGithub(_pull(repo, spec)).client(),
        env={"CODEX_API_KEY": "x", "REVIEW_CLASSIFIER_API_KEY": "y"},
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "SPEC_NOT_FOUND"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace("  - src/**", "  - src/**\n  - docs/**"),
        lambda text: text.replace(
            "### Validation\n\n```text\n",
            "### Validation\n\n```text\ntrue\n",
        ),
        lambda text: text.replace(
            "# Objective\n\nKeep src/app.py valid.",
            "# Objective\n\nReplace the Task Spec body under the same id.",
        ),
    ],
    ids=["allowed_paths", "validation", "same_id_rewrite"],
)
def test_run_review_rejects_spec_body_changes(tmp_path: Path, mutate) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    path = repo / "specs" / "tasks" / "review-demo.md"
    path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8")
    result = run_review(
        repo_root=repo,
        pull_number=7,
        head_sha_expected=head_sha(repo),
        spec_path="specs/tasks/review-demo.md",
        github=FakeGithub(_pull(repo, spec)).client(),
        env={"CODEX_API_KEY": "x", "REVIEW_CLASSIFIER_API_KEY": "y"},
    )
    assert result.outcome == "ESCALATED"
    assert result.code == "SPEC_IDENTITY_MISMATCH"


def test_prepare_review_rejects_digest_mismatch(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    files = _spec_files(repo)
    files["specs/tasks/review-demo.md"] = files["specs/tasks/review-demo.md"].replace(
        "- File exists.",
        "- File exists and is renamed.",
    )
    fake = FakeGithub(_pull(repo, spec), files=files)
    with pytest.raises(AgentError) as exc_info:
        prepare_review(
            repo_root=repo,
            event_payload=_check_run_event(repo),
            repository="octo/repo",
            github=fake.client(),
        )
    assert exc_info.value.code == "SPEC_IDENTITY_MISMATCH"


def test_review_continues_when_only_head_sha_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    original_digest = spec.spec_sha256
    (repo / "src" / "app.py").write_text("repaired\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-m", "repair")
    current = parse_spec(repo / "specs" / "tasks" / "review-demo.md")
    assert current.spec_sha256 == original_digest
    fake = FakeGithub(_pull(repo, spec))
    fake.add_check_run(head_sha=head_sha(repo))
    result = _run(repo, fake)
    assert result.outcome == "READY_FOR_HUMAN"
    assert result.code != "SPEC_IDENTITY_MISMATCH"


def test_review_track_version_1_does_not_reset_attempts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    v1_body = "\n".join(
        [
            "<!-- md-agent-review-state",
            "schema_version: 1",
            f"spec_id: {spec.id}",
            f"base_branch: {spec.base_branch}",
            f"target_branch: {spec.target_branch}",
            "head_sha: abc",
            "review_attempts: 4",
            "processed:",
            "- keep-me",
            "-->",
        ]
    )
    fake = FakeGithub(_pull(repo, spec))
    fake.issue_comments.append(
        {
            "id": 9,
            "body": v1_body,
            "user": {"login": "github-actions[bot]"},
        }
    )
    with pytest.raises(AgentError) as exc_info:
        load_review_track(
            fake.client(),
            7,
            spec,
            track_author="github-actions[bot]",
        )
    assert exc_info.value.code == "UNSAFE_REVIEW_TRACK"
    result = _run(repo, fake)
    assert result.outcome == "ESCALATED"
    assert result.code == "UNSAFE_REVIEW_TRACK"
    assert any(
        "review_attempts: 4" in str(comment.get("body") or "")
        and "keep-me" in str(comment.get("body") or "")
        for comment in fake.issue_comments
    )
    assert not any(
        "schema_version: 2" in str(comment.get("body") or "") for comment in fake.issue_comments
    )


def test_review_track_version_2_identity_mismatch_is_unsafe(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    spec = _spec(repo)
    track = empty_review_track(spec)
    body = render_review_track(track).replace(spec.spec_sha256, "f" * 64)
    fake = FakeGithub(_pull(repo, spec))
    fake.issue_comments.append(
        {
            "id": 9,
            "body": body,
            "user": {"login": "github-actions[bot]"},
        }
    )
    result = _run(repo, fake)
    assert result.outcome == "ESCALATED"
    assert result.code == "UNSAFE_REVIEW_TRACK"
    assert any(
        comment.get("id") == 9 and "f" * 64 in str(comment.get("body") or "")
        for comment in fake.issue_comments
    )
