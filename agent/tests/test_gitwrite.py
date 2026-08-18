from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from agent.errors import AgentError
from agent.gitutil import require_git_ok, run_git
from agent.gitwrite import (
    apply_patch,
    commit_paths,
    create_branch_from_sha,
    export_patch,
    head_sha,
    push_branch,
    run_git_write,
)


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "p6@example.com")
    _git(repo, "config", "user.name", "Phase6")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def test_export_apply_and_commit_without_force(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    src = repo / "src"
    src.mkdir()
    (src / "app.py").write_text("ok\n", encoding="utf-8")
    gitignore = repo / ".gitignore"
    gitignore.write_text(".agent/state/*.json\n", encoding="utf-8")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-m", "ignore state")
    state_dir = repo / ".agent" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "leaked.json").write_text("{}\n", encoding="utf-8")
    base = head_sha(repo)
    patch = tmp_path / "changes.patch"
    export_patch(repo, base, patch)
    text = patch.read_text(encoding="utf-8")
    assert "app.py" in text
    assert "leaked.json" in text

    other = tmp_path / "other"
    subprocess.run(["git", "clone", str(repo), str(other)], check=True, capture_output=True)
    apply_patch(other, patch)
    sha = commit_paths(other, ["src/app.py"], "feat(demo): complete task-1")
    assert sha
    log = _git(other, "log", "-1", "--format=%s")
    assert log == "feat(demo): complete task-1"
    assert (other / "src" / "app.py").read_text(encoding="utf-8") == "ok\n"


def test_force_push_is_rejected(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(AgentError) as exc_info:
        run_git_write(repo, "push", "--force", "origin", "HEAD")
    assert exc_info.value.code == "FORBIDDEN_GIT"


def test_push_branch_to_local_origin(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    _git(repo, "remote", "add", "origin", str(origin))
    create_branch_from_sha(repo, "feature/demo", head_sha(repo))
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    commit_paths(repo, ["src/app.py"], "feat(demo): complete task-1")
    push_branch(repo, "feature/demo")
    listed = require_git_ok(run_git(origin, "branch"), "branch")
    assert "feature/demo" in listed


def test_push_injects_https_auth_without_github_token_in_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    repo = _repo(tmp_path)
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare")
    _git(repo, "remote", "add", "origin", str(origin))
    create_branch_from_sha(repo, "feature/demo", head_sha(repo))
    captured: dict[str, object] = {}
    real = subprocess.run

    def fake_run(*args: object, **kwargs: object):
        captured["argv"] = args[0] if args else kwargs.get("args")
        captured["env"] = kwargs.get("env")
        return real(*args, **kwargs)

    monkeypatch.setattr("agent.gitwrite.subprocess.run", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_push_token")
    push_branch(repo, "feature/demo")
    env = captured["env"]
    assert isinstance(env, dict)
    assert "GITHUB_TOKEN" not in env
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    encoded = base64.b64encode(b"x-access-token:ghs_test_push_token").decode("ascii")
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {encoded}"
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:2] == ["git", "push"]


def test_commit_does_not_receive_push_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("ok\n", encoding="utf-8")
    captured: list[dict[str, str]] = []
    real = subprocess.run

    def fake_run(*args: object, **kwargs: object):
        env = kwargs.get("env") or {}
        if isinstance(env, dict):
            captured.append(env)
        return real(*args, **kwargs)

    monkeypatch.setattr("agent.gitwrite.subprocess.run", fake_run)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test_push_token")
    commit_paths(repo, ["src/app.py"], "feat(demo): complete task-1")
    assert captured
    for env in captured:
        assert "GITHUB_TOKEN" not in env
        assert "GIT_CONFIG_KEY_0" not in env
