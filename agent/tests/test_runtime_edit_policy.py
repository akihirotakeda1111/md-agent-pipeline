from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from agent.codex_runner import ProcessResult, build_implementation_prompt
from agent.config import RuntimeEditPolicy, load_config
from agent.cycle import run_task_cycle
from agent.errors import AgentError
from agent.gitutil import GitChange
from agent.gitwrite import head_sha
from agent.intake import prepare_execute
from agent.repair import build_repair_prompt
from agent.review_prompt import build_review_repair_prompt
from agent.scope import check_scope, path_is_in_scope, validate_spec_scope_policy
from agent.spec import parse_spec, parse_spec_text
from agent.state import (
    ExecutionStatus,
    fingerprint_state_file,
    new_execution_state,
    read_state,
    state_file_path,
    write_state,
)
from agent.validation import ValidationRecord
from agent.workunit import run_work_unit

SPEC_TEMPLATE = """---
schema_version: 1
id: policy-demo
title: Policy Demo
status: PENDING
base_branch: main
target_branch: feature/policy
allowed_paths:
{allowed}
forbidden_paths:
{forbidden}
repair_attempt_limit: {limit}
review_attempt_limit: 1
---

# Objective

Write src/app.py.

# Non-Goals

None.

# Forbidden Actions

None.

# Architecture Invariants

Keep src.

# Tasks

## task-1: Write app

### Requirement

Create src/app.py with ok.

### Acceptance Criteria

- File exists.

### Validation

```text
python3 check_app.py
```

# Final Verification

```text
python3 check_app.py
```
"""


def _policy() -> RuntimeEditPolicy:
    return load_config().runtime_edit_policy


def _yaml_list(values: tuple[str, ...] | list[str]) -> str:
    if not values:
        return "  []"
    return "\n".join(f'  - "{item}"' for item in values)


def _spec_text(
    *,
    allowed: tuple[str, ...] = ("src/**",),
    forbidden: tuple[str, ...] = ("src/secret/**",),
    limit: int = 1,
) -> str:
    return SPEC_TEMPLATE.format(
        allowed=_yaml_list(allowed),
        forbidden=_yaml_list(forbidden),
        limit=limit,
    )


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr


def _env() -> dict[str, str]:
    python_dir = str(Path(sys.executable).parent)
    path = os.environ.get("PATH", "")
    if python_dir not in path.split(os.pathsep):
        path = python_dir + os.pathsep + path
    env = {"PATH": path}
    if os.environ.get("PATHEXT"):
        env["PATHEXT"] = os.environ["PATHEXT"]
    if os.environ.get("SYSTEMROOT"):
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return env


def _init_repo(
    tmp_path: Path,
    *,
    allowed: tuple[str, ...] = ("src/**",),
    forbidden: tuple[str, ...] = ("src/secret/**",),
    limit: int = 1,
) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "rf01@example.com")
    _git(repo, "config", "user.name", "RF01")
    (repo / "check_app.py").write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "path = Path('src/app.py')\n"
        "sys.exit(0 if path.is_file() and path.read_text() == 'ok\\n' else 1)\n",
        encoding="utf-8",
    )
    spec_path = repo / "spec.md"
    spec_path.write_text(
        _spec_text(allowed=allowed, forbidden=forbidden, limit=limit),
        encoding="utf-8",
    )
    _git(repo, "add", "check_app.py", "spec.md")
    _git(repo, "commit", "-m", "init")
    return repo, spec_path


def _write_ok(cwd: str) -> None:
    dest = Path(cwd) / "src" / "app.py"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("ok\n", encoding="utf-8")


def test_protected_outranks_allowed() -> None:
    spec = parse_spec_text(_spec_text(allowed=("src/**", "agent/config.json", "specs/**")))
    policy = _policy()
    assert path_is_in_scope("src/app.py", spec, policy)
    assert not path_is_in_scope("agent/config.json", spec, policy)
    assert not path_is_in_scope("specs/tasks/x.md", spec, policy)


def test_forbidden_outranks_allowed() -> None:
    spec = parse_spec_text(_spec_text(allowed=("src/**", "src/secret/**")))
    assert not path_is_in_scope("src/secret/key.txt", spec, _policy())


def test_unspecified_path_is_default_deny() -> None:
    spec = parse_spec_text(_spec_text())
    assert not path_is_in_scope("docs/readme.md", spec, _policy())


def test_starstar_allowed_still_cannot_edit_protected_paths() -> None:
    spec = parse_spec_text(_spec_text(allowed=("**",), forbidden=()))
    policy = _policy()
    result = check_scope(
        spec,
        [
            GitChange(path="src/app.py", status="modified"),
            GitChange(path="agent/config.json", status="modified"),
            GitChange(path="specs/tasks/x.md", status="added"),
            GitChange(path=".agent/state/other.json", status="added"),
            GitChange(path=".github/workflows/x.yml", status="modified"),
        ],
        policy,
    )
    assert result.allowed is False
    assert "src/app.py" not in result.violation_paths
    assert "agent/config.json" in result.violation_paths
    assert "specs/tasks/x.md" in result.violation_paths
    assert ".agent/state/other.json" in result.violation_paths
    assert ".github/workflows/x.yml" in result.violation_paths


def test_preflight_rejects_clear_protected_overlaps() -> None:
    policy = _policy()
    for allowed in (
        ("**",),
        ("agent/**",),
        (".agent/**",),
        ("specs/**",),
        (".github/**",),
        ("agent/config.json",),
    ):
        spec = parse_spec_text(_spec_text(allowed=allowed, forbidden=()))
        with pytest.raises(AgentError) as exc_info:
            validate_spec_scope_policy(spec, policy)
        assert exc_info.value.code == "INVALID_SPEC"


def test_preflight_rejects_exact_file_under_protected_directory() -> None:
    spec = parse_spec_text(_spec_text(allowed=("src/**", ".github/workflows/ci.yml")))
    with pytest.raises(AgentError) as exc_info:
        validate_spec_scope_policy(spec, _policy())
    assert exc_info.value.code == "INVALID_SPEC"


def test_preflight_allows_complex_existing_globs() -> None:
    spec = parse_spec_text(_spec_text(allowed=("src/**", "**/*.ts", "docs/**/*.md")))
    validate_spec_scope_policy(spec, _policy())
    validate_spec_scope_policy(spec, _policy())


def test_semantic_guard_is_idempotent() -> None:
    spec = parse_spec_text(_spec_text())
    policy = _policy()
    validate_spec_scope_policy(spec, policy)
    validate_spec_scope_policy(spec, policy)
    invalid = parse_spec_text(_spec_text(allowed=("**",), forbidden=()))
    with pytest.raises(AgentError) as first:
        validate_spec_scope_policy(invalid, policy)
    with pytest.raises(AgentError) as second:
        validate_spec_scope_policy(invalid, policy)
    assert str(first.value) == str(second.value)


def test_run_task_cycle_runs_semantic_guard(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path, allowed=("**",), forbidden=())

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("codex must not start")

    with pytest.raises(AgentError) as exc_info:
        run_task_cycle(
            spec_path,
            repo_root=repo,
            env=_env(),
            executor=executor,
            persist_state=False,
        )
    assert exc_info.value.code == "INVALID_SPEC"


def test_run_work_unit_runs_semantic_guard(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path, allowed=("**",), forbidden=())

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("codex must not start")

    with pytest.raises(AgentError) as exc_info:
        run_work_unit(
            spec_path,
            repo_root=repo,
            report_dir=tmp_path / "out",
            env=_env(),
            executor=executor,
            persist_state=False,
        )
    assert exc_info.value.code == "INVALID_SPEC"


def test_prepare_execute_runs_semantic_guard(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path, allowed=("agent/**",), forbidden=())
    with pytest.raises(AgentError) as exc_info:
        prepare_execute(spec_path, repo_root=repo)
    assert exc_info.value.code == "INVALID_SPEC"


def test_work_unit_loads_config_once_and_reuses_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, spec_path = _init_repo(tmp_path)
    cfg = load_config()
    loads = {"n": 0}

    def counting_load(*_args: object, **_kwargs: object):
        loads["n"] += 1
        raise AssertionError("config must not be reloaded")

    monkeypatch.setattr("agent.workunit.load_config", counting_load)
    monkeypatch.setattr("agent.cycle.load_config", counting_load)
    monkeypatch.setattr("agent.codex_runner.load_config", counting_load)
    monkeypatch.setattr("agent.state.load_config", counting_load)

    seen: list[object] = []
    orig = run_task_cycle

    def wrapped(spec: object, **kwargs: object):
        seen.append(kwargs.get("config"))
        return orig(spec, **kwargs)

    monkeypatch.setattr("agent.workunit.run_task_cycle", wrapped)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        return ProcessResult(0, "done", "")

    report = run_work_unit(
        spec_path,
        repo_root=repo,
        report_dir=tmp_path / "out",
        config=cfg,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert report.outcome == "FINAL_VERIFICATION_PASSED"
    assert loads["n"] == 0
    assert len(seen) >= 2
    assert all(item is cfg for item in seen)


def test_prompt_and_scope_check_use_the_same_policy_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, spec_path = _init_repo(tmp_path)
    cfg = load_config()
    seen: dict[str, object] = {}
    orig_prompt = build_implementation_prompt
    orig_scope = check_scope

    def wrap_prompt(spec: object, task: object, *, repo_root: object, runtime_policy: object):
        seen["prompt"] = runtime_policy
        return orig_prompt(spec, task, repo_root=repo_root, runtime_policy=runtime_policy)

    def wrap_scope(spec: object, changes: object, runtime_policy: object):
        seen["scope"] = runtime_policy
        return orig_scope(spec, changes, runtime_policy)

    monkeypatch.setattr("agent.codex_runner.build_implementation_prompt", wrap_prompt)
    monkeypatch.setattr("agent.cycle.check_scope", wrap_scope)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        (Path(cwd) / "agent").mkdir(exist_ok=True)
        (Path(cwd) / "agent" / "config.json").write_text("{}", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        config=cfg,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert seen["prompt"] is cfg.runtime_edit_policy
    assert seen["scope"] is cfg.runtime_edit_policy
    assert result.outcome == "SCOPE_VIOLATION"
    assert "agent/config.json" in (result.scope.violation_paths if result.scope else ())


def test_codex_config_rewrite_does_not_reload_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, spec_path = _init_repo(tmp_path, allowed=("src/**", "docs/*.md"))
    cfg = replace(
        load_config(),
        runtime_edit_policy=RuntimeEditPolicy(
            protected_paths=(*load_config().runtime_edit_policy.protected_paths, "docs/**")
        ),
    )
    monkeypatch.setattr(
        "agent.cycle.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reloaded")),
    )
    monkeypatch.setattr(
        "agent.codex_runner.load_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("reloaded")),
    )

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        dest = Path(cwd) / "docs" / "secret.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("nope\n", encoding="utf-8")
        config_path = Path(cwd) / "agent" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(
                {
                    "task_spec": {"directory": "specs/tasks"},
                    "state": {"directory": ".agent/state"},
                    "runtime_edit_policy": {"protected_paths": ["agent/**"]},
                }
            ),
            encoding="utf-8",
        )
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        config=cfg,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert "docs/secret.md" in result.scope.violation_paths


def test_implementation_rejects_protected_path(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        dest = Path(cwd) / "specs" / "leaked.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("nope\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert "specs/leaked.md" in result.scope.violation_paths


def test_validation_repair_rejects_protected_path(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path, limit=2)
    calls = {"n": 0}

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        calls["n"] += 1
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("bad\n", encoding="utf-8")
        if calls["n"] > 1:
            leaked = Path(cwd) / ".github" / "workflows" / "x.yml"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            leaked.write_text("nope\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert ".github/workflows/x.yml" in result.scope.violation_paths
    assert calls["n"] == 2


def test_prompts_share_protected_forbidden_allowed_sections(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    task = spec.tasks[0]
    policy = _policy()
    implement = build_implementation_prompt(spec, task, repo_root=repo, runtime_policy=policy)
    repair = build_repair_prompt(
        spec,
        task,
        repo_root=repo,
        failed=ValidationRecord(
            task_id=task.id,
            command="true",
            argv=("true",),
            exit_code=1,
            stdout="",
            stderr="boom",
            duration_ms=1,
            timed_out=False,
            denied=False,
        ),
        diff_text="",
        runtime_policy=policy,
    )
    review = build_review_repair_prompt(
        spec,
        repo_root=repo,
        base_sha=head_sha(repo),
        accepted=(),
        current_task=task,
        runtime_policy=policy,
    )
    for prompt in (implement, repair, review):
        assert "# Repository Protected Paths" in prompt
        assert "# Task-level Forbidden Paths" in prompt
        assert "# Allowed Paths" in prompt
        assert "Protected > Forbidden > Allowed" in prompt
        assert "unless those paths are allowed" not in prompt


def test_fingerprint_regular_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"ok": true}\n', encoding="utf-8")
    first = fingerprint_state_file(path)
    second = fingerprint_state_file(path)
    assert first == second
    assert first.exists is True
    assert first.file_type == "regular"
    assert first.content_sha256
    assert first.symlink_target is None


def test_fingerprint_does_not_follow_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("secret-bytes\n", encoding="utf-8")
    link = tmp_path / "state.json"
    link.symlink_to(target)
    fingerprint = fingerprint_state_file(link)
    assert fingerprint.file_type == "symlink"
    assert fingerprint.symlink_target == str(target)
    assert fingerprint.content_sha256 is None
    retarget = tmp_path / "other.json"
    retarget.write_text("secret-bytes\n", encoding="utf-8")
    link.unlink()
    link.symlink_to(retarget)
    changed = fingerprint_state_file(link)
    assert changed != fingerprint
    assert changed.symlink_target == str(retarget)


def test_matching_current_state_is_excluded_but_other_state_is_not(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        extra = Path(cwd) / ".agent" / "state" / "other-task.json"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("{}\n", encoding="utf-8")
        tmp = Path(cwd) / ".agent" / "state" / f"{spec.id}.json.tmp"
        tmp.write_text("{}\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "SCOPE_VIOLATION"
    assert f".agent/state/{spec.id}.json" not in result.scope.violation_paths
    assert ".agent/state/other-task.json" in result.scope.violation_paths
    assert f".agent/state/{spec.id}.json.tmp" in result.scope.violation_paths


def test_current_state_content_change_is_state_tampered(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        path = Path(cwd) / ".agent" / "state" / f"{spec.id}.json"
        path.write_text('{"tampered": true}\n', encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"
    assert "STATE_TAMPERED" in result.message
    on_disk = (repo / ".agent" / "state" / f"{spec.id}.json").read_text(encoding="utf-8")
    assert on_disk == '{"tampered": true}\n'
    assert '"SCOPE_VIOLATION"' not in on_disk


def test_current_state_deletion_is_state_tampered(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        (Path(cwd) / ".agent" / "state" / f"{spec.id}.json").unlink()
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"
    assert not (repo / ".agent" / "state" / f"{spec.id}.json").exists()


def test_current_state_replaced_with_symlink_is_state_tampered(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        path = Path(cwd) / ".agent" / "state" / f"{spec.id}.json"
        payload = path.read_text(encoding="utf-8")
        decoy = Path(cwd) / ".agent" / "state" / "decoy.json"
        decoy.write_text(payload, encoding="utf-8")
        path.unlink()
        path.symlink_to(decoy)
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"
    assert (repo / ".agent" / "state" / f"{spec.id}.json").is_symlink()


def _plant_valid_state_symlink(repo: Path, spec) -> tuple[Path, Path, str]:
    state_dir = repo / ".agent" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    decoy = state_dir / "decoy.json"
    write_state(decoy, new_execution_state(spec))
    link = state_dir / f"{spec.id}.json"
    link.symlink_to(decoy)
    return link, decoy, decoy.read_text(encoding="utf-8")


def test_pre_codex_symlink_state_fails_closed(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    state_dir = repo / ".agent" / "state"
    state_dir.mkdir(parents=True)
    decoy = state_dir / "decoy.json"
    decoy.write_text("{}\n", encoding="utf-8")
    (state_dir / f"{spec.id}.json").symlink_to(decoy)

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("codex must not start")

    result = run_task_cycle(
        spec,
        repo_root=repo,
        env=_env(),
        executor=executor,
        state=new_execution_state(spec),
        persist_state=False,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"


def test_persist_true_symlink_to_valid_state_fails_closed_before_io(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    link, decoy, decoy_text = _plant_valid_state_symlink(repo, spec)
    target_before = os.readlink(link)

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("codex must not start")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"
    assert "STATE_TAMPERED" in result.message
    assert link.is_symlink()
    assert os.readlink(link) == target_before
    assert decoy.read_text(encoding="utf-8") == decoy_text


def test_work_unit_persist_true_symlink_to_valid_state_fails_closed(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    link, decoy, decoy_text = _plant_valid_state_symlink(repo, spec)
    target_before = os.readlink(link)

    def executor(*_args: object, **_kwargs: object) -> ProcessResult:
        raise AssertionError("codex must not start")

    report = run_work_unit(
        spec_path,
        repo_root=repo,
        report_dir=tmp_path / "out",
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert report.outcome == "SCOPE_VIOLATION"
    assert "STATE_TAMPERED" in report.message
    assert report.scope_allowed is False
    assert link.is_symlink()
    assert os.readlink(link) == target_before
    assert decoy.read_text(encoding="utf-8") == decoy_text


def test_persist_state_false_does_not_write_state(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "TASK_COMPLETED"
    assert not state_file_path(repo, spec.id).exists()


def test_creating_current_state_during_persist_false_is_tamper(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        path = Path(cwd) / ".agent" / "state" / f"{spec.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n", encoding="utf-8")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=False,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"


def test_matching_fingerprint_allows_in_scope_change(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert result.outcome == "TASK_COMPLETED"
    disk = read_state(state_file_path(repo, spec.id))
    assert disk.state is ExecutionStatus.TASK_COMPLETED


def test_implementation_exception_after_state_change_is_state_tampered(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        path = Path(cwd) / ".agent" / "state" / f"{spec.id}.json"
        path.write_text('{"tampered": true}\n', encoding="utf-8")
        raise RuntimeError("codex crashed")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"
    on_disk = (repo / ".agent" / "state" / f"{spec.id}.json").read_text(encoding="utf-8")
    assert on_disk == '{"tampered": true}\n'
    assert '"SCOPE_VIOLATION"' not in on_disk


def test_repair_exception_after_state_change_is_state_tampered(tmp_path: Path) -> None:
    repo, spec_path = _init_repo(tmp_path, limit=2)
    spec = parse_spec(spec_path)
    calls = {"n": 0}

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        calls["n"] += 1
        dest = Path(cwd) / "src" / "app.py"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("bad\n", encoding="utf-8")
        if calls["n"] > 1:
            path = Path(cwd) / ".agent" / "state" / f"{spec.id}.json"
            path.write_text('{"tampered": true}\n', encoding="utf-8")
            raise RuntimeError("repair crashed")
        return ProcessResult(0, "done", "")

    result = run_task_cycle(
        spec_path,
        repo_root=repo,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert calls["n"] == 2
    assert result.outcome == "SCOPE_VIOLATION"
    assert result.scope is not None
    assert result.scope.reason == "STATE_TAMPERED"
    on_disk = (repo / ".agent" / "state" / f"{spec.id}.json").read_text(encoding="utf-8")
    assert on_disk == '{"tampered": true}\n'
    assert '"SCOPE_VIOLATION"' not in on_disk


def test_work_unit_persist_true_reuses_custom_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent.cycle as cycle_mod
    import agent.reconcile as reconcile_mod

    repo, spec_path = _init_repo(tmp_path)
    spec = parse_spec(spec_path)
    cfg = replace(load_config())
    loads = {"n": 0}

    def counting_load(*_args: object, **_kwargs: object):
        loads["n"] += 1
        raise AssertionError("config must not be reloaded")

    monkeypatch.setattr("agent.workunit.load_config", counting_load)
    monkeypatch.setattr("agent.cycle.load_config", counting_load)
    monkeypatch.setattr("agent.codex_runner.load_config", counting_load)
    monkeypatch.setattr("agent.state.load_config", counting_load)

    seen_configs: list[object] = []
    seen_policies: list[object] = []

    def _record_config(args: tuple[object, ...], kwargs: dict[str, object]) -> object:
        if "config" in kwargs:
            return kwargs["config"]
        if len(args) >= 3:
            return args[2]
        return None

    orig_cycle_sfp = cycle_mod.state_file_path
    orig_reconcile_sfp = reconcile_mod.state_file_path
    orig_scope = cycle_mod.check_scope

    def wrap_cycle_sfp(*args: object, **kwargs: object):
        seen_configs.append(_record_config(args, kwargs))
        return orig_cycle_sfp(*args, **kwargs)

    def wrap_reconcile_sfp(*args: object, **kwargs: object):
        seen_configs.append(_record_config(args, kwargs))
        return orig_reconcile_sfp(*args, **kwargs)

    def wrap_scope(spec_obj: object, changes: object, runtime_policy: object):
        seen_policies.append(runtime_policy)
        return orig_scope(spec_obj, changes, runtime_policy)

    monkeypatch.setattr("agent.cycle.state_file_path", wrap_cycle_sfp)
    monkeypatch.setattr("agent.reconcile.state_file_path", wrap_reconcile_sfp)
    monkeypatch.setattr("agent.cycle.check_scope", wrap_scope)

    def executor(*_args: object, cwd: str, **_kwargs: object) -> ProcessResult:
        _write_ok(cwd)
        return ProcessResult(0, "done", "")

    report = run_work_unit(
        spec_path,
        repo_root=repo,
        report_dir=tmp_path / "out",
        config=cfg,
        env=_env(),
        executor=executor,
        persist_state=True,
    )
    assert report.outcome == "FINAL_VERIFICATION_PASSED"
    assert loads["n"] == 0
    assert seen_configs
    assert all(item is cfg for item in seen_configs)
    assert seen_policies
    assert all(item is cfg.runtime_edit_policy for item in seen_policies)
    disk = read_state(state_file_path(repo, spec.id, config=cfg))
    assert disk.task_id == spec.id
