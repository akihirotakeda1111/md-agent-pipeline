#!/usr/bin/env python3
"""Dispatch real production Agent Execute runs and assert job conclusions."""

from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SUITE = Path(__file__).resolve().parent
REPO_ROOT = SUITE.parents[2]
if str(SUITE) not in sys.path:
    sys.path.insert(0, str(SUITE))

from integration.expectations import check_run  # noqa: E402
from integration.reporting import (  # noqa: E402
    empty_case_report,
    extract_cycle_result,
    finalize_case_report,
    inspect_codex_api_key_presence,
    redact_codex_api_key,
    write_reports,
)
from integration.runs import (  # noqa: E402
    check_run_identity,
    match_triggered_runs,
    require_unique_run,
)
from integration.spec_template import render_spec  # noqa: E402

WORKFLOW = "agent-execute.yml"
GIT_EMAIL = "phase5-it@example.com"
GIT_NAME = "phase5-it"


def run_cmd(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd or REPO_ROOT),
        check=check,
        capture_output=True,
        text=True,
    )


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_cmd(["gh", *args], check=check)


def git(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_cmd(["git", *args], cwd=worktree, check=check)


def git_commit(worktree: Path, *args: str) -> None:
    git(
        worktree,
        "-c",
        f"user.email={GIT_EMAIL}",
        "-c",
        f"user.name={GIT_NAME}",
        "commit",
        *args,
    )


def default_branch(repo: str) -> str:
    payload = json.loads(gh("repo", "view", repo, "--json", "defaultBranchRef").stdout)
    name = payload["defaultBranchRef"]["name"]
    if not name:
        raise RuntimeError("could not resolve default branch")
    return str(name)


def unique_suffix() -> str:
    return secrets.token_hex(4)


def fetch_failed_log(repo: str, run_id: int) -> str:
    completed = gh("run", "view", str(run_id), "--repo", repo, "--log-failed", check=False)
    chunks = [completed.stdout or "", completed.stderr or ""]
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def attach_failed_evidence(report: dict[str, Any], repo: str) -> None:
    run_id = report.get("run_id")
    if not run_id:
        return
    try:
        raw = fetch_failed_log(repo, int(run_id))
    except Exception as exc:
        errors = [*(report.get("errors") or []), f"failed to fetch job log: {exc}"]
        report["errors"] = errors
        return
    if not raw:
        return
    report["codex_api_key_present"] = inspect_codex_api_key_presence(raw)
    redacted = redact_codex_api_key(raw)
    report["failed_log"] = redacted
    report["cycle_result"] = extract_cycle_result(redacted)


def add_spec(worktree: Path, relative: str, contents: str) -> None:
    path = worktree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", newline="\n")
    posix = relative.replace("\\", "/")
    git(worktree, "add", "--", posix)
    git_commit(worktree, "-m", f"phase5-it: {posix}")


def list_run_ids(repo: str) -> set[int]:
    raw = gh(
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        WORKFLOW,
        "--limit",
        "50",
        "--json",
        "databaseId",
    ).stdout
    return {int(item["databaseId"]) for item in json.loads(raw)}


def list_runs_for_commit(
    repo: str,
    *,
    branch: str,
    event: str,
    head_sha: str,
) -> list[dict[str, Any]]:
    raw = gh(
        "run",
        "list",
        "--repo",
        repo,
        "--workflow",
        WORKFLOW,
        "--branch",
        branch,
        "--commit",
        head_sha,
        "--event",
        event,
        "--limit",
        "20",
        "--json",
        "databaseId,event,headSha,headBranch,createdAt,url,status",
    ).stdout
    return list(json.loads(raw))


def locate_run(
    repo: str,
    *,
    branch: str,
    event: str,
    head_sha: str,
    known_ids: set[int],
    timeout: float,
) -> int:
    if not head_sha:
        raise ValueError("head_sha is required to uniquely identify a workflow run")
    deadline = time.monotonic() + timeout
    last_error = "no matching run yet"
    while time.monotonic() < deadline:
        items = list_runs_for_commit(repo, branch=branch, event=event, head_sha=head_sha)
        matches = match_triggered_runs(
            items,
            branch=branch,
            event=event,
            head_sha=head_sha,
            known_ids=known_ids,
        )
        try:
            return require_unique_run(matches, branch=branch, event=event, head_sha=head_sha)
        except LookupError as exc:
            last_error = str(exc)
            time.sleep(2)
        except RuntimeError:
            raise
    raise RuntimeError(
        f"timed out locating unique {event} run for {branch}@{head_sha}: {last_error}"
    )


def assert_watched_run(
    repo: str,
    case: dict[str, Any],
    run_id: int,
    *,
    branch: str,
    head_sha: str,
) -> dict[str, Any]:
    gh("run", "watch", str(run_id), "--repo", repo, "--exit-status", check=False)
    data = json.loads(
        gh(
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--json",
            "conclusion,jobs,url,event,headSha,headBranch",
        ).stdout
    )
    errors = check_run(
        expected_conclusion=str(case["expected_conclusion"]),
        expected_parse=str(case["parse_job"]),
        expected_execute=str(case["execute_job"]),
        expected_event=str(case["event"]),
        conclusion=data.get("conclusion"),
        jobs=list(data.get("jobs") or []),
        event=data.get("event"),
    )
    errors.extend(
        check_run_identity(
            expected_event=str(case["event"]),
            expected_branch=branch,
            expected_sha=head_sha,
            event=data.get("event"),
            branch=data.get("headBranch"),
            sha=data.get("headSha"),
        )
    )
    report = {
        "run_id": run_id,
        "url": data.get("url"),
        "conclusion": data.get("conclusion"),
        "event": data.get("event"),
        "head_sha": data.get("headSha"),
        "branch": data.get("headBranch"),
        "jobs": {job.get("name"): job.get("conclusion") for job in data.get("jobs") or []},
        "errors": errors,
    }
    return report


def create_worktree(branch: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix="phase5-it-"))
    shutil.rmtree(path)
    run_cmd(["git", "worktree", "add", "-b", branch, str(path), "HEAD"], cwd=REPO_ROOT)
    return path


def cleanup_branch(worktree: Path | None, branch: str, *, keep: bool) -> None:
    if keep:
        print(f"keeping branch {branch}", file=sys.stderr)
        return
    origin = worktree if worktree is not None and worktree.exists() else REPO_ROOT
    run_cmd(["git", "push", "origin", "--delete", branch], cwd=origin, check=False)
    if worktree is not None and worktree.exists():
        run_cmd(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=REPO_ROOT,
            check=False,
        )
    run_cmd(["git", "branch", "-D", branch], cwd=REPO_ROOT, check=False)
    run_cmd(["git", "worktree", "prune"], cwd=REPO_ROOT, check=False)


def render_case_files(case: dict[str, Any], *, default_ref: str) -> tuple[str, str, str, str]:
    suffix = unique_suffix()
    case_key = str(case["id"]).split("-", 1)[0]
    task_id = f"p5it{case_key}{suffix}"
    branch = f"agent/p5it-{case_key}-{suffix}"
    base_branch = branch if case["base_branch_mode"] == "self" else default_ref
    relative = f"specs/tasks/_it-{task_id}.md"
    template = (SUITE / "fixtures" / str(case["fixture"])).read_text(encoding="utf-8")
    contents = render_spec(
        template,
        task_id=task_id,
        base_branch=base_branch,
        target_branch=branch,
    )
    return task_id, branch, relative, contents


def run_push_case(
    repo: str,
    case: dict[str, Any],
    *,
    default_ref: str,
    locate_timeout: float,
    keep_branch: bool,
) -> dict[str, Any]:
    report = empty_case_report(case)
    task_id, branch, relative, contents = render_case_files(case, default_ref=default_ref)
    report["task_id"] = task_id
    report["spec_path"] = relative
    report["branch"] = branch
    worktree: Path | None = None
    try:
        worktree = create_worktree(branch)
        add_spec(worktree, relative, contents)
        known = list_run_ids(repo)
        git(worktree, "push", "-u", "origin", branch)
        head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
        report["head_sha"] = head_sha
        run_id = locate_run(
            repo,
            branch=branch,
            event="push",
            head_sha=head_sha,
            known_ids=known,
            timeout=locate_timeout,
        )
        report["run_id"] = run_id
        report.update(assert_watched_run(repo, case, run_id, branch=branch, head_sha=head_sha))
        report["task_id"] = task_id
        report["spec_path"] = relative
        if report.get("errors"):
            attach_failed_evidence(report, repo)
    except Exception as exc:
        report.setdefault("errors", [])
        report["errors"] = [*(report.get("errors") or []), str(exc)]
        if report.get("run_id"):
            attach_failed_evidence(report, repo)
    finally:
        cleanup_branch(worktree, branch, keep=keep_branch)
    return finalize_case_report(report)


def run_dispatch_case(
    repo: str,
    case: dict[str, Any],
    *,
    default_ref: str,
    locate_timeout: float,
    keep_branch: bool,
) -> dict[str, Any]:
    report = empty_case_report(case)
    task_id, branch, relative, contents = render_case_files(case, default_ref=default_ref)
    report["task_id"] = task_id
    report["spec_path"] = relative
    report["branch"] = branch
    worktree: Path | None = None
    try:
        worktree = create_worktree(branch)
        add_spec(worktree, relative, contents)
        git(worktree, "push", "-u", "origin", branch)
        head_sha = git(worktree, "rev-parse", "HEAD").stdout.strip()
        report["head_sha"] = head_sha
        known = list_run_ids(repo)
        gh(
            "workflow",
            "run",
            WORKFLOW,
            "--repo",
            repo,
            "--ref",
            branch,
            "-f",
            f"spec_path={relative}",
        )
        run_id = locate_run(
            repo,
            branch=branch,
            event="workflow_dispatch",
            head_sha=head_sha,
            known_ids=known,
            timeout=locate_timeout,
        )
        report["run_id"] = run_id
        report.update(assert_watched_run(repo, case, run_id, branch=branch, head_sha=head_sha))
        report["task_id"] = task_id
        report["spec_path"] = relative
        if report.get("errors"):
            attach_failed_evidence(report, repo)
    except Exception as exc:
        report.setdefault("errors", [])
        report["errors"] = [*(report.get("errors") or []), str(exc)]
        if report.get("run_id"):
            attach_failed_evidence(report, repo)
    finally:
        cleanup_branch(worktree, branch, keep=keep_branch)
    return finalize_case_report(report)


def verify_contract() -> None:
    script = SUITE / "integration" / "verify_contract.py"
    completed = run_cmd([sys.executable, str(script)], cwd=REPO_ROOT)
    if completed.stdout:
        print(completed.stdout, end="")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Phase 5 cases against production agent-execute.yml"
    )
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument(
        "--locate-timeout",
        type=float,
        default=90.0,
        help="seconds to wait for the GitHub Actions run to appear",
    )
    parser.add_argument(
        "--keep-branch",
        action="store_true",
        help="leave ephemeral branches for debugging",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=SUITE / "reports",
    )
    args = parser.parse_args()
    verify_contract()
    config = json.loads((SUITE / "cases.json").read_text(encoding="utf-8"))
    cases = list(config["cases"])
    if args.case_ids:
        wanted = set(args.case_ids)
        cases = [case for case in cases if case["id"] in wanted]
        missing = wanted - {case["id"] for case in cases}
        if missing:
            print(f"Unknown case(s): {', '.join(sorted(missing))}", file=sys.stderr)
            return 2
    default_ref = default_branch(args.repo)
    reports_dir = args.report_dir
    rows: list[dict[str, Any]] = []
    failed = False
    for case in cases:
        case_id = str(case["id"])
        if case["trigger"] == "workflow_dispatch":
            report = run_dispatch_case(
                args.repo,
                case,
                default_ref=default_ref,
                locate_timeout=args.locate_timeout,
                keep_branch=args.keep_branch,
            )
        else:
            report = run_push_case(
                args.repo,
                case,
                default_ref=default_ref,
                locate_timeout=args.locate_timeout,
                keep_branch=args.keep_branch,
            )
        rows.append(report)
        if report.get("status") == "PASS":
            print(f"PASS {case_id}: {report.get('url')}")
        else:
            failed = True
            print(
                f"FAIL {case_id}: "
                f"{'; '.join(redact_codex_api_key(item) for item in report.get('errors') or [])}",
                file=sys.stderr,
            )
    write_reports(rows, reports_dir.resolve())
    print(f"Reports: {reports_dir.resolve()}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
