from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from .coderabbit_terminal import (
    KIND_COMPLETED,
    KIND_FAILED,
    KIND_SKIPPED,
    resolve_coderabbit_terminal,
)
from .models import (
    EnvironmentBlocker,
    ExternalServiceBlocker,
    FeedbackEvidence,
    PullRequestEvidence,
    RunEvidence,
    WorkflowInfo,
)
from .process import CommandError, run


ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested", "waiting_for_review"}
)
TERMINAL_WAKE_EVENTS = frozenset({"check_run", "status"})


class GitHub:
    def __init__(self, repo: str, *, poll_seconds: int = 10) -> None:
        self.repo = repo
        self.owner = repo.split("/", 1)[0]
        self.poll_seconds = poll_seconds

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
        try:
            run(["gh", "auth", "status"])
            viewer = self.api("user")
            data = self.api(f"repos/{self.repo}")
        except (CommandError, json.JSONDecodeError) as exc:
            raise EnvironmentBlocker(f"GitHub authentication/repository preflight failed: {exc}") from exc
        permissions = data.get("permissions") or {}
        if not permissions.get("push"):
            raise EnvironmentBlocker("Harness credential does not have repository push permission")
        return {**data, "viewer_login": str(viewer.get("login") or "")}

    def workflow(self, workflow_file: str, triggers: tuple[str, ...]) -> WorkflowInfo:
        data = self.api(f"repos/{self.repo}/actions/workflows/{workflow_file}")
        if data.get("state") != "active":
            raise EnvironmentBlocker(f"Production workflow is not active: {workflow_file}")
        return WorkflowInfo(
            id=int(data["id"]),
            name=str(data["name"]),
            path=str(data["path"]),
            state=str(data["state"]),
            triggers=triggers,
        )

    def dispatch(self, workflow_id: int, ref: str, inputs: dict[str, str]) -> None:
        self.api(
            f"repos/{self.repo}/actions/workflows/{workflow_id}/dispatches",
            method="POST",
            payload={"ref": ref, "inputs": inputs},
            expect_json=False,
        )

    def list_workflow_runs(self, workflow_id: int, *, event: str | None = None) -> list[dict[str, Any]]:
        fields = {"per_page": "100"}
        if event:
            fields["event"] = event
        data = self.api(f"repos/{self.repo}/actions/workflows/{workflow_id}/runs", fields=fields)
        return list(data.get("workflow_runs", []))

    def workflow_run_ids(self, workflow_id: int) -> set[int]:
        return {int(item["id"]) for item in self.list_workflow_runs(workflow_id)}

    def matching_runs(
        self,
        workflow_id: int,
        branch: str,
        sha: str,
        event: str,
        *,
        exclude_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        skipped = exclude_ids or set()
        return [
            item
            for item in self.list_workflow_runs(workflow_id, event=event)
            if item.get("head_branch") == branch
            and item.get("head_sha") == sha
            and item.get("event") == event
            and int(item["id"]) not in skipped
        ]

    def discover_unique_execute_run(
        self,
        workflow_id: int,
        branch: str,
        sha: str,
        event: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            matches = self.matching_runs(workflow_id, branch, sha, event)
            if len(matches) > 1:
                raise AssertionError(
                    f"ambiguous execute run: branch={branch} sha={sha} event={event} count={len(matches)}"
                )
            if len(matches) == 1:
                return matches[0]
            time.sleep(min(3, self.poll_seconds))
        raise ExternalServiceBlocker(
            f"execute workflow run not found for branch={branch} sha={sha} event={event}"
        )

    def wait_new_review_run(
        self,
        workflow_id: int,
        baseline_ids: set[int],
        *,
        timeout_seconds: int,
        event: str | None = None,
        actor: str | None = None,
        correlation_text: str | None = None,
        head_sha: str | None = None,
        allow_terminal_events: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        observed: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            candidates = [
                item
                for item in self.list_workflow_runs(workflow_id, event=event)
                if int(item["id"]) not in baseline_ids
            ]
            observed = [
                {
                    "id": str(item.get("id")),
                    "event": str(item.get("event")),
                    "actor": str((item.get("actor") or {}).get("login") or ""),
                    "display_title": str(item.get("display_title") or ""),
                    "head_sha": str(item.get("head_sha") or ""),
                }
                for item in candidates
            ]
            if actor is not None:
                candidates = [
                    item
                    for item in candidates
                    if str((item.get("actor") or {}).get("login") or "") == actor
                    or (
                        allow_terminal_events
                        and str(item.get("event") or "") in TERMINAL_WAKE_EVENTS
                    )
                ]
            if correlation_text:
                token = correlation_text.lower()
                candidates = [
                    item
                    for item in candidates
                    if token in str(item.get("display_title") or "").lower()
                    or (head_sha is not None and str(item.get("head_sha") or "") == head_sha)
                ]
            elif head_sha is not None:
                candidates = [
                    item for item in candidates if str(item.get("head_sha") or "") == head_sha
                ]
            if candidates:
                return sorted(candidates, key=lambda item: str(item.get("created_at") or ""))[0]
            time.sleep(self.poll_seconds)
        raise ExternalServiceBlocker(
            "review workflow run was not observed before timeout",
            evidence={
                "event": event,
                "actor": actor,
                "correlation_text": correlation_text,
                "head_sha": head_sha,
                "observed_new_runs": observed,
            },
        )

    def wait_attempt(self, run_id: int, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self.api(f"repos/{self.repo}/actions/runs/{run_id}")
            if data.get("status") == "completed":
                return data
            time.sleep(self.poll_seconds)
        raise ExternalServiceBlocker(f"workflow run {run_id} did not complete before timeout")

    def run_evidence(self, data: dict[str, Any]) -> RunEvidence:
        run_id = int(data["id"])
        attempt = int(data.get("run_attempt", 1))
        jobs_data = self.api(f"repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs")
        jobs = {
            str(item["name"]): str(item.get("conclusion")) for item in jobs_data.get("jobs", [])
        }
        _, events = self.attempt_events(run_id, attempt)
        return RunEvidence(
            id=run_id,
            attempt=attempt,
            workflow_url=str(data["html_url"]),
            sha=str(data.get("head_sha") or ""),
            branch=str(data.get("head_branch") or ""),
            event=str(data.get("event") or ""),
            actor=str((data.get("actor") or {}).get("login") or ""),
            status=str(data.get("status") or ""),
            conclusion=str(data.get("conclusion") or ""),
            jobs=jobs,
            events=tuple(events),
        )

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

    def pulls(self, head_branch: str, base_branch: str, *, state: str = "open") -> list[dict[str, Any]]:
        return self.api(
            f"repos/{self.repo}/pulls",
            fields={
                "state": state,
                "head": f"{self.owner}:{head_branch}",
                "base": base_branch,
                "per_page": "100",
            },
        )

    def pr_evidence(self, number_or_data: int | dict[str, Any]) -> PullRequestEvidence:
        number = number_or_data if isinstance(number_or_data, int) else int(number_or_data["number"])
        details = self.api(f"repos/{self.repo}/pulls/{number}")
        return PullRequestEvidence(
            number=int(details["number"]),
            url=str(details["html_url"]),
            head=str(details["head"]["ref"]),
            base=str(details["base"]["ref"]),
            head_sha=str(details["head"]["sha"]),
            body=str(details.get("body") or ""),
            labels=tuple(str(label["name"]) for label in details.get("labels", [])),
            state=str(details.get("state") or ""),
            merged=bool(details.get("merged")),
            merged_at=details.get("merged_at"),
            auto_merge=details.get("auto_merge"),
        )

    def list_feedback(self, number: int) -> list[FeedbackEvidence]:
        items: list[FeedbackEvidence] = []
        for kind, path in (
            ("review", f"repos/{self.repo}/pulls/{number}/reviews"),
            ("review_comment", f"repos/{self.repo}/pulls/{number}/comments"),
            ("issue_comment", f"repos/{self.repo}/issues/{number}/comments"),
        ):
            raw_items = self.api(path, fields={"per_page": "100"})
            for item in raw_items:
                user = item.get("user") or {}
                commit_sha = item.get("commit_id") or item.get("original_commit_id")
                items.append(
                    FeedbackEvidence(
                        kind=kind,
                        source_id=int(item["id"]),
                        actor=str(user.get("login") or ""),
                        body=str(item.get("body") or ""),
                        path=str(item["path"]) if item.get("path") is not None else None,
                        commit_sha=str(commit_sha) if commit_sha else None,
                        created_at=str(item.get("created_at") or item.get("submitted_at") or ""),
                        updated_at=str(item.get("updated_at") or item.get("submitted_at") or ""),
                        html_url=str(item.get("html_url")) if item.get("html_url") else None,
                    )
                )
        return items

    def wait_for_coderabbit_feedback(
        self,
        number: int,
        actor: str,
        *,
        head_sha: str,
        known_ids: set[tuple[str, int, str]],
        timeout_seconds: int,
    ) -> list[FeedbackEvidence]:
        deadline = time.monotonic() + timeout_seconds
        observed_actors: set[str] = set()
        while time.monotonic() < deadline:
            feedback = self.list_feedback(number)
            fresh = [
                item
                for item in feedback
                if (item.kind, item.source_id, item.updated_at) not in known_ids
            ]
            observed_actors.update(item.actor for item in fresh if item.actor)
            matching = [item for item in fresh if item.actor == actor]
            if matching:
                # Some issue comments/reviews are not bound to a commit by GitHub's API.
                # We record that limitation but require at least one real event from the configured actor.
                return matching
            time.sleep(self.poll_seconds)
        raise ExternalServiceBlocker(
            "configured CodeRabbit actor did not produce feedback before timeout",
            evidence={
                "configured_actor": actor,
                "head_sha": head_sha,
                "observed_actors": sorted(observed_actors),
            },
        )

    def list_check_runs(self, sha: str) -> list[dict[str, Any]]:
        data = self.api(
            f"repos/{self.repo}/commits/{sha}/check-runs",
            fields={"per_page": "100"},
        )
        if not isinstance(data, dict):
            return []
        runs = data.get("check_runs")
        return [item for item in runs if isinstance(item, dict)] if isinstance(runs, list) else []

    def list_commit_statuses(self, sha: str) -> list[dict[str, Any]]:
        data = self.api(
            f"repos/{self.repo}/commits/{sha}/statuses",
            fields={"per_page": "100"},
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    def coderabbit_terminal(
        self,
        sha: str,
        *,
        actor: str,
        check_app_slug: str,
        status_context: str,
    ) -> dict[str, str]:
        return resolve_coderabbit_terminal(
            self.list_check_runs(sha),
            self.list_commit_statuses(sha),
            head_sha=sha,
            actor=actor,
            check_app_slug=check_app_slug,
            status_context=status_context,
        )

    def wait_for_scenario_a_signal(
        self,
        *,
        pr_number: int,
        head_sha: str,
        actor: str,
        check_app_slug: str,
        status_context: str,
        known_ids: set[tuple[str, int, str]],
        workflow_id: int,
        baseline_ids: set[int],
        timeout_seconds: int,
        correlation_text: str,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            runs = [
                item
                for item in self.list_workflow_runs(workflow_id)
                if int(item["id"]) not in baseline_ids
            ]
            matching_runs = []
            token = correlation_text.lower()
            for item in runs:
                event_name = str(item.get("event") or "")
                run_actor = str((item.get("actor") or {}).get("login") or "")
                title = str(item.get("display_title") or "").lower()
                run_sha = str(item.get("head_sha") or "")
                actor_ok = run_actor == actor or event_name in TERMINAL_WAKE_EVENTS
                correlated = token in title or run_sha == head_sha
                if actor_ok and correlated:
                    matching_runs.append(item)
            if matching_runs:
                chosen = sorted(matching_runs, key=lambda item: str(item.get("created_at") or ""))[0]
                return {"kind": "run", "run": chosen}

            terminal = self.coderabbit_terminal(
                head_sha,
                actor=actor,
                check_app_slug=check_app_slug,
                status_context=status_context,
            )
            if terminal["kind"] in {KIND_COMPLETED, KIND_SKIPPED, KIND_FAILED}:
                return {"kind": "terminal", "terminal": terminal}

            feedback = [
                item
                for item in self.list_feedback(pr_number)
                if (item.kind, item.source_id, item.updated_at) not in known_ids
                and item.actor == actor
            ]
            if feedback:
                return {"kind": "feedback", "items": feedback}
            time.sleep(self.poll_seconds)
        raise ExternalServiceBlocker(
            "CodeRabbit terminal, feedback, or review run was not observed before timeout",
            evidence={"head_sha": head_sha, "configured_actor": actor},
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

    def compare(self, before: str, after: str) -> dict[str, Any]:
        return self.api(f"repos/{self.repo}/compare/{before}...{after}")

    def tracking_comments(self, number: int, marker: str) -> list[dict[str, Any]]:
        comments = self.api(
            f"repos/{self.repo}/issues/{number}/comments", fields={"per_page": "100"}
        )
        return [item for item in comments if marker in str(item.get("body") or "")]

    def create_non_coderabbit_wakeup(
        self,
        number: int,
        *,
        supported_events: tuple[str, ...],
        head_sha: str,
        generated_file: str,
        marker: str,
    ) -> dict[str, Any]:
        if "issue_comment" in supported_events:
            item = self.api(
                f"repos/{self.repo}/issues/{number}/comments",
                method="POST",
                payload={"body": marker},
            )
            return {"event": "issue_comment", "kind": "issue_comment", "id": item.get("id")}
        if "pull_request_review_comment" in supported_events:
            item = self.api(
                f"repos/{self.repo}/pulls/{number}/comments",
                method="POST",
                payload={
                    "body": marker,
                    "commit_id": head_sha,
                    "path": generated_file,
                    "line": 1,
                    "side": "RIGHT",
                },
            )
            return {
                "event": "pull_request_review_comment",
                "kind": "review_comment",
                "id": item.get("id"),
            }
        if "pull_request_review" in supported_events:
            item = self.api(
                f"repos/{self.repo}/pulls/{number}/reviews",
                method="POST",
                payload={"body": marker, "event": "COMMENT", "commit_id": head_sha},
            )
            return {
                "event": "pull_request_review",
                "kind": "review",
                "id": item.get("id"),
            }
        raise RuntimeError("review workflow exposes no safe event for Scenario B")

    def close_pr(self, number: int) -> None:
        self.api(f"repos/{self.repo}/pulls/{number}", method="PATCH", fields={"state": "closed"})

    def delete_branch(self, branch: str) -> None:
        self.api(
            f"repos/{self.repo}/git/refs/heads/{branch}",
            method="DELETE",
            expect_json=False,
        )

    @staticmethod
    def utc_now() -> str:
        return datetime.now(UTC).isoformat()
