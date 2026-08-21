from __future__ import annotations

import json
import time
from typing import Any

from .models import PullRequestEvidence, RunEvidence, WorkflowInfo
from .process import run

ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested", "waiting_for_review"}
)


class GitHub:
    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.owner = repo.split("/", 1)[0]

    def api(
        self,
        path: str,
        *,
        method: str = "GET",
        fields: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        expect_json: bool = True,
        check: bool = True,
    ) -> Any:
        command = ["gh", "api", "--method", method, path]
        for key, value in (fields or {}).items():
            command.extend(["-f", f"{key}={value}"])
        input_text = None
        if payload is not None:
            command.extend(["--input", "-"])
            input_text = json.dumps(payload)
        completed = run(command, check=check, input_text=input_text)
        if not expect_json or not completed.stdout.strip():
            return None
        return json.loads(completed.stdout)

    def preflight(self) -> dict[str, Any]:
        run(["gh", "auth", "status"])
        data = self.api(f"repos/{self.repo}")
        permissions = data.get("permissions") or {}
        if not permissions.get("push"):
            raise PermissionError("Harness credential does not have repository push permission")
        return data

    def workflow(
        self, workflow_file: str, trigger: str, dispatch_inputs: dict[str, Any]
    ) -> WorkflowInfo:
        data = self.api(f"repos/{self.repo}/actions/workflows/{workflow_file}")
        if data.get("state") != "active":
            raise AssertionError(f"Production workflow is not active: {data.get('state')}")
        return WorkflowInfo(
            id=int(data["id"]),
            name=str(data["name"]),
            path=str(data["path"]),
            state=str(data["state"]),
            trigger=trigger,
            dispatch_inputs=dispatch_inputs,
        )

    def dispatch(self, workflow_id: int, ref: str, inputs: dict[str, str]) -> None:
        self.api(
            f"repos/{self.repo}/actions/workflows/{workflow_id}/dispatches",
            method="POST",
            payload={"ref": ref, "inputs": inputs},
            expect_json=False,
        )

    def matching_runs(
        self,
        workflow_id: int,
        branch: str,
        sha: str,
        event: str,
        *,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        data = self.api(
            f"repos/{self.repo}/actions/workflows/{workflow_id}/runs",
            fields={"branch": branch, "event": event, "per_page": "100"},
        )
        skipped = exclude_ids or set()
        return [
            item
            for item in data.get("workflow_runs", [])
            if item.get("head_branch") == branch
            and item.get("head_sha") == sha
            and item.get("event") == event
            and int(item["id"]) not in skipped
        ]

    def discover_unique_run(
        self,
        workflow_id: int,
        branch: str,
        sha: str,
        event: str,
        timeout_seconds: int,
        *,
        exclude_ids: set[int] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            matches = self.matching_runs(workflow_id, branch, sha, event, exclude_ids=exclude_ids)
            if len(matches) > 1:
                raise AssertionError(
                    "ambiguous workflow run: "
                    f"workflow={workflow_id} branch={branch} sha={sha} "
                    f"event={event} count={len(matches)}"
                )
            if len(matches) == 1:
                return matches[0]
            time.sleep(3)
        raise AssertionError(
            "workflow run not found: "
            f"workflow={workflow_id} branch={branch} sha={sha} event={event}"
        )

    def wait_attempt(
        self, run_id: int, minimum_attempt: int, timeout_seconds: int
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self.api(f"repos/{self.repo}/actions/runs/{run_id}")
            if (
                int(data.get("run_attempt", 0)) >= minimum_attempt
                and data.get("status") == "completed"
            ):
                return data
            time.sleep(10)
        raise TimeoutError(f"workflow run {run_id} attempt {minimum_attempt} did not complete")

    def run_evidence(self, data: dict[str, Any]) -> RunEvidence:
        run_id = int(data["id"])
        attempt = int(data.get("run_attempt", 1))
        jobs_data = self.api(f"repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs")
        jobs = {
            str(item["name"]): str(item.get("conclusion")) for item in jobs_data.get("jobs", [])
        }
        return RunEvidence(
            id=run_id,
            attempt=attempt,
            workflow_url=str(data["html_url"]),
            sha=str(data["head_sha"]),
            branch=str(data["head_branch"]),
            event=str(data["event"]),
            conclusion=str(data.get("conclusion")),
            jobs=jobs,
        )

    def cancel_stuck_run(self, run_id: int, timeout_seconds: int) -> dict[str, Any]:
        data = self.api(f"repos/{self.repo}/actions/runs/{run_id}")
        status = str(data.get("status") or "")
        attempt = int(data.get("run_attempt") or 1)
        if status not in ACTIVE_RUN_STATUSES:
            return {"cancelled": False, "status": status, "attempt": attempt}
        completed = run(
            ["gh", "api", "--method", "POST", f"repos/{self.repo}/actions/runs/{run_id}/cancel"],
            check=False,
        )
        requested = completed.returncode == 0
        if completed.returncode != 0:
            detail = f"{completed.stdout}\n{completed.stderr}".lower()
            if "409" not in detail and "cannot cancel" not in detail:
                raise RuntimeError(f"failed to cancel workflow run {run_id}: {detail[-1000:]}")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self.api(f"repos/{self.repo}/actions/runs/{run_id}")
            status = str(data.get("status") or "")
            attempt = int(data.get("run_attempt") or 1)
            if status not in ACTIVE_RUN_STATUSES:
                return {"cancelled": requested, "status": status, "attempt": attempt}
            time.sleep(3)
        raise TimeoutError(f"workflow run {run_id} did not stop after cancel")

    def open_pulls(self, head_branch: str, base_branch: str) -> list[dict[str, Any]]:
        return self.api(
            f"repos/{self.repo}/pulls",
            fields={
                "state": "open",
                "head": f"{self.owner}:{head_branch}",
                "base": base_branch,
                "per_page": "100",
            },
        )

    def pr_evidence(self, data: dict[str, Any]) -> PullRequestEvidence:
        details = self.api(f"repos/{self.repo}/pulls/{data['number']}")
        return PullRequestEvidence(
            number=int(details["number"]),
            url=str(details["html_url"]),
            head=str(details["head"]["ref"]),
            base=str(details["base"]["ref"]),
            head_sha=str(details["head"]["sha"]),
            body=str(details.get("body") or ""),
            labels=tuple(str(label["name"]) for label in details.get("labels", [])),
            author=str((details.get("user") or {}).get("login") or ""),
        )

    def branch_sha(self, branch: str) -> str:
        data = self.api(f"repos/{self.repo}/git/ref/heads/{branch}")
        return str(data["object"]["sha"])

    def branch_exists(self, branch: str) -> bool:
        completed = run(
            ["gh", "api", "--method", "GET", f"repos/{self.repo}/git/ref/heads/{branch}"],
            check=False,
        )
        if completed.returncode == 0:
            return True
        detail = f"{completed.stdout}\n{completed.stderr}".lower()
        if "http 404" in detail or "not found" in detail:
            return False
        raise RuntimeError(f"failed to inspect branch {branch}: {detail[-1000:]}")

    def commit_files(self, sha: str) -> list[str]:
        data = self.api(f"repos/{self.repo}/commits/{sha}")
        return sorted(str(item["filename"]) for item in data.get("files", []))

    def pr_files(self, number: int) -> list[str]:
        data = self.api(f"repos/{self.repo}/pulls/{number}/files", fields={"per_page": "100"})
        return sorted(str(item["filename"]) for item in data)

    def attempt_events(self, run_id: int, attempt: int) -> tuple[str, list[str]]:
        completed = run(
            [
                "gh",
                "run",
                "view",
                str(run_id),
                "--repo",
                self.repo,
                "--attempt",
                str(attempt),
                "--log",
            ],
            check=False,
        )
        if completed.returncode != 0:
            return "unavailable", []
        events: list[str] = []
        for line in completed.stdout.splitlines():
            brace = line.find("{")
            if brace < 0:
                continue
            try:
                payload = json.loads(line[brace:])
            except json.JSONDecodeError:
                continue
            name = payload.get("event", payload.get("type")) if isinstance(payload, dict) else None
            if isinstance(name, str):
                events.append(name)
        return ("observed" if events else "not_observable"), events

    def close_pr(self, number: int) -> None:
        self.api(f"repos/{self.repo}/pulls/{number}", method="PATCH", fields={"state": "closed"})

    def delete_branch(self, branch: str) -> None:
        self.api(
            f"repos/{self.repo}/git/refs/heads/{branch}",
            method="DELETE",
            expect_json=False,
        )
