from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from .assertions import _job_matches, review_run_completed, run_matches_current_head, terminal_state
from .coderabbit_terminal import (
    KIND_AMBIGUOUS,
    KIND_COMPLETED,
    KIND_FAILED,
    KIND_IN_PROGRESS,
    KIND_NONE,
    KIND_SKIPPED,
    resolve_coderabbit_terminal,
    status_context_matches,
)
from .models import (
    ClassifiedFailure,
    EnvironmentBlocker,
    ExternalServiceBlocker,
    FeedbackEvidence,
    ProductionBug,
    PullRequestEvidence,
    RunEvidence,
    WorkflowInfo,
)
from .process import CommandError, run
from .workflow import COMMENT_REVIEW_EVENTS


ACTIVE_RUN_STATUSES = frozenset(
    {"queued", "in_progress", "waiting", "pending", "requested", "waiting_for_review"}
)
TERMINAL_WAKE_EVENTS = frozenset({"check_run", "status"})
CANDIDATE_PENDING_PREPARE = "pending_prepare"
CANDIDATE_OTHER_PR = "other_pr"
CANDIDATE_STALE_HEAD = "stale_head"
CANDIDATE_PREPARE_SKIPPED = "prepare_skipped"
CANDIDATE_PREPARE_FAILED = "prepare_failed"
CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE = "prepare_output_unavailable"
CANDIDATE_REVIEW_PENDING = "review_pending"
CANDIDATE_REVIEW_EXECUTED = "review_executed"
REVIEW_SIGNAL_KINDS = frozenset({CANDIDATE_REVIEW_PENDING, CANDIDATE_REVIEW_EXECUTED})
PREPARE_FAULT_KINDS = frozenset(
    {CANDIDATE_PREPARE_FAILED, CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE}
)
_STABLE_CANDIDATE_KINDS = frozenset(
    {
        CANDIDATE_OTHER_PR,
        CANDIDATE_STALE_HEAD,
        CANDIDATE_PREPARE_SKIPPED,
        CANDIDATE_PREPARE_FAILED,
        CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE,
        CANDIDATE_REVIEW_EXECUTED,
    }
)
_SKIPPED_JOB_CONCLUSIONS = frozenset({"skipped", "cancelled", "neutral"})
_PREPARE_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})


def _check_app_slug(item: dict[str, Any]) -> str:
    app = item.get("app")
    if not isinstance(app, dict):
        return ""
    return str(app.get("slug") or "").strip()


def _compact_check_run(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "conclusion": item.get("conclusion"),
        "app_slug": _check_app_slug(item),
        "head_sha": item.get("head_sha"),
        "completed_at": item.get("completed_at"),
    }


def _compact_commit_status(item: dict[str, Any]) -> dict[str, Any]:
    creator = item.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    return {
        "context": item.get("context"),
        "state": item.get("state"),
        "sha": item.get("sha"),
        "description": item.get("description"),
        "creator": login,
        "updated_at": item.get("updated_at") or item.get("created_at"),
    }


def _status_matches(item: dict[str, Any], *, actor: str, status_context: str) -> bool:
    context = item.get("context")
    creator = item.get("creator")
    login = creator.get("login") if isinstance(creator, dict) else None
    context_ok = isinstance(context, str) and status_context_matches(context, status_context)
    actor_ok = isinstance(login, str) and login.strip() == actor.strip()
    return context_ok or actor_ok


def parse_prepare_pull_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def should_review_enabled(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def prepare_binds_to_target_pr(
    prepare: dict[str, Any] | None,
    *,
    pr_number: int,
    head_sha: str,
) -> bool:
    if not prepare:
        return False
    number = parse_prepare_pull_number(prepare.get("pull_number"))
    if number != pr_number:
        return False
    sha = str(prepare.get("head_sha") or "").strip().lower()
    expected = head_sha.strip().lower()
    return bool(sha) and bool(expected) and sha == expected


def new_terminal_wake_runs(
    runs: list[dict[str, Any]], baseline_ids: set[int]
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for item in runs:
        try:
            run_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if run_id in baseline_ids:
            continue
        if str(item.get("event") or "") not in TERMINAL_WAKE_EVENTS:
            continue
        matched.append(item)
    return matched


def review_job_executed(jobs: dict[str, str], *, run_status: str) -> bool:
    conclusions = [
        str(conclusion or "")
        for name, conclusion in jobs.items()
        if _job_matches(name, "review and repair")
    ]
    if not conclusions:
        return False
    active = run_status in ACTIVE_RUN_STATUSES
    for conclusion in conclusions:
        lowered = conclusion.lower()
        if lowered in _SKIPPED_JOB_CONCLUSIONS:
            continue
        if lowered in {"", "none"}:
            return active
        return True
    return False


def prepare_job_conclusion(jobs: dict[str, str]) -> str | None:
    matches = [
        str(conclusion or "")
        for name, conclusion in jobs.items()
        if _job_matches(name, "prepare review")
    ]
    if not matches:
        return None
    return matches[0]


def _is_prepare_job_failure(conclusion: str | None) -> bool:
    return str(conclusion or "").lower() in _PREPARE_FAILURE_CONCLUSIONS


def classify_terminal_wake_candidate(
    *,
    prepare: dict[str, Any] | None,
    jobs: dict[str, str],
    run_status: str,
    pr_number: int,
    head_sha: str,
    prepare_log_error: bool = False,
) -> str:
    prep_job = prepare_job_conclusion(jobs)
    if prepare is not None:
        number = parse_prepare_pull_number(prepare.get("pull_number"))
        if number != pr_number:
            return CANDIDATE_OTHER_PR
        prepare_head = str(prepare.get("head_sha") or "").strip().lower()
        current = head_sha.strip().lower()
        if not prepare_head or prepare_head != current:
            return CANDIDATE_STALE_HEAD
        if _is_prepare_job_failure(prep_job):
            return CANDIDATE_PREPARE_FAILED
        if not should_review_enabled(prepare.get("should_review")):
            return CANDIDATE_PREPARE_SKIPPED
        if review_job_executed(jobs, run_status=run_status):
            return CANDIDATE_REVIEW_EXECUTED
        if run_status in ACTIVE_RUN_STATUSES:
            return CANDIDATE_REVIEW_PENDING
        return CANDIDATE_PREPARE_SKIPPED
    if run_status in ACTIVE_RUN_STATUSES:
        return CANDIDATE_PENDING_PREPARE
    if not jobs:
        return CANDIDATE_PENDING_PREPARE
    if _is_prepare_job_failure(prep_job):
        return CANDIDATE_PREPARE_FAILED
    if str(prep_job or "").lower() in _SKIPPED_JOB_CONCLUSIONS:
        return CANDIDATE_PREPARE_SKIPPED
    if prepare_log_error or str(prep_job or "").lower() == "success":
        return CANDIDATE_PREPARE_OUTPUT_UNAVAILABLE
    return CANDIDATE_PENDING_PREPARE


def review_runs_from_classified(classified: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item["run"]
        for item in classified
        if item.get("kind") in REVIEW_SIGNAL_KINDS and isinstance(item.get("run"), dict)
    ]


def choose_unseen_review_run(
    classified: list[dict[str, Any]], *, seen_ids: set[int]
) -> dict[str, Any] | None:
    unseen = [
        item
        for item in review_runs_from_classified(classified)
        if int(item["id"]) not in seen_ids
    ]
    if not unseen:
        return None
    return sorted(unseen, key=lambda item: str(item.get("created_at") or ""))[0]


def completed_review_run_for_terminal(
    classified: list[dict[str, Any]],
    *,
    current_head: str,
    pr_head_sha: str,
    terminal: str | None,
) -> dict[str, Any] | None:
    review_runs = review_runs_from_classified(classified)
    active = [
        item for item in review_runs if str(item.get("status") or "") in ACTIVE_RUN_STATUSES
    ]
    if pr_head_sha != current_head or active:
        return None
    completed = [
        item for item in review_runs if str(item.get("status") or "") == "completed"
    ]
    if terminal not in {"READY_FOR_HUMAN", "ESCALATED", "FAILED"} or not completed:
        return None
    return sorted(
        completed,
        key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
    )[-1]


def raise_if_prepare_fault(snapshot: dict[str, Any]) -> None:
    kind = snapshot.get("kind")
    if kind not in PREPARE_FAULT_KINDS:
        return
    evidence = {"candidate": candidate_evidence_row(snapshot)}
    if kind == CANDIDATE_PREPARE_FAILED:
        raise ProductionBug("Agent Review prepare job failed", evidence=evidence)
    if snapshot.get("prepare_log_error"):
        raise EnvironmentBlocker(
            "could not read Agent Review prepare output from workflow logs",
            evidence=evidence,
        )
    raise ProductionBug(
        "Agent Review prepare completed without usable JSON output",
        evidence=evidence,
    )


CODERABBIT_TERMINAL_KINDS = frozenset(
    {KIND_COMPLETED, KIND_SKIPPED, KIND_FAILED, KIND_AMBIGUOUS}
)
PRODUCTION_TERMINAL_STATES = frozenset({"READY_FOR_HUMAN", "ESCALATED", "FAILED"})


def _candidate_classification(item: dict[str, Any]) -> str:
    return str(item.get("classification") or item.get("kind") or "")


def classify_scenario_a_timeout(
    *,
    pr: PullRequestEvidence | None,
    current_head: str,
    latest_review_run: RunEvidence | None,
    coderabbit_terminal: dict[str, str] | None,
    candidate_runs: list[dict[str, Any]],
    feedback_count: int,
    unprocessed_feedback: bool = False,
    wait_evidence: dict[str, Any] | None = None,
) -> ClassifiedFailure:
    evidence = dict(wait_evidence or {})
    evidence.setdefault("current_head", current_head)
    evidence.setdefault("candidate_runs", candidate_runs)
    evidence["unprocessed_feedback"] = unprocessed_feedback
    if coderabbit_terminal is not None:
        evidence["coderabbit_terminal"] = coderabbit_terminal
    if latest_review_run is not None:
        evidence["latest_review_run"] = {
            "id": latest_review_run.id,
            "conclusion": latest_review_run.conclusion,
            "event": latest_review_run.event,
            "events": list(latest_review_run.events),
            "bound_head_sha": getattr(latest_review_run, "bound_head_sha", ""),
        }
    state = terminal_state(pr) if pr is not None else None
    kind = str(
        (coderabbit_terminal or evidence.get("coderabbit_terminal") or {}).get("kind")
        or KIND_NONE
    )
    started = any(
        _candidate_classification(item) not in {CANDIDATE_OTHER_PR, CANDIDATE_STALE_HEAD, ""}
        for item in candidate_runs
    )
    activity = kind not in {KIND_NONE, ""} or feedback_count > 0
    current_run = run_matches_current_head(latest_review_run, current_head)
    current_run_completed = current_run and review_run_completed(latest_review_run)

    if (
        kind == KIND_SKIPPED
        and state == "READY_FOR_HUMAN"
        and current_run
        and latest_review_run is not None
        and "READY_FOR_HUMAN" in latest_review_run.events
    ):
        return ProductionBug(
            "Production READY_FOR_HUMAN after CodeRabbit Review skipped",
            evidence={**evidence, "failure_kind": "SKIPPED_TREATED_AS_COMPLETED"},
        )
    if state in PRODUCTION_TERMINAL_STATES and current_run_completed:
        if kind in CODERABBIT_TERMINAL_KINDS and not started:
            return ProductionBug(
                "CodeRabbit terminal was observed but Agent Review workflow did not start (transport failure)",
                evidence={**evidence, "failure_kind": "TRANSPORT_FAILURE"},
            )
        return ProductionBug(
            f"Production {state} on the current HEAD did not match review run structured events",
            evidence={**evidence, "failure_kind": "UNMATCHED_PRODUCTION_TERMINAL"},
        )
    if not activity and not started:
        return ExternalServiceBlocker(
            "CodeRabbit produced no check, commit status, or feedback on the current HEAD",
            evidence={**evidence, "failure_kind": "NO_CODERABBIT_ACTIVITY"},
        )
    if kind in {KIND_NONE, KIND_IN_PROGRESS}:
        failure_kind = (
            "CODERABBIT_IN_PROGRESS" if kind == KIND_IN_PROGRESS else "EVIDENCE_INCOMPLETE"
        )
        return ExternalServiceBlocker(
            "CodeRabbit terminal evidence on the current HEAD is missing or still in progress",
            evidence={**evidence, "failure_kind": failure_kind},
        )
    if kind in CODERABBIT_TERMINAL_KINDS and not started:
        return ProductionBug(
            "CodeRabbit terminal was observed but Agent Review workflow did not start (transport failure)",
            evidence={**evidence, "failure_kind": "TRANSPORT_FAILURE"},
        )
    if (
        kind == KIND_COMPLETED
        and not unprocessed_feedback
        and current_run
        and latest_review_run is not None
        and latest_review_run.conclusion == "success"
        and state is None
    ):
        return ProductionBug(
            "CodeRabbit COMPLETED with no unprocessed feedback but Production remained IN_REVIEW",
            evidence={**evidence, "failure_kind": "SUCCESS_STILL_IN_REVIEW"},
        )
    return ExternalServiceBlocker(
        "Phase 7 convergence deadline expired before a Production terminal",
        evidence={**evidence, "failure_kind": "CONVERGENCE_TIMEOUT"},
    )


def candidate_evidence_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
    prepare = snapshot.get("prepare") if isinstance(snapshot.get("prepare"), dict) else None
    jobs = snapshot.get("jobs") if isinstance(snapshot.get("jobs"), dict) else {}
    return {
        "id": snapshot.get("id") if snapshot.get("id") is not None else run.get("id"),
        "event": snapshot.get("event") or run.get("event"),
        "html_url": snapshot.get("html_url") or run.get("html_url"),
        "run_status": snapshot.get("status") or run.get("status"),
        "run_conclusion": (
            snapshot.get("conclusion") if "conclusion" in snapshot else run.get("conclusion")
        ),
        "prepare_job_conclusion": prepare_job_conclusion(jobs),
        "prepare_output_available": prepare is not None,
        "prepare_output_error": bool(snapshot.get("prepare_log_error")),
        "should_review": None if prepare is None else prepare.get("should_review"),
        "reason": None if prepare is None else prepare.get("reason"),
        "pull_number": None if prepare is None else prepare.get("pull_number"),
        "head_sha": None if prepare is None else prepare.get("head_sha"),
        "run_head_sha": snapshot.get("run_head_sha") or run.get("head_sha"),
        "classification": snapshot.get("kind"),
        "prepare": prepare,
        "jobs": jobs,
    }


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

    def wait_without_comment_review_run(
        self,
        workflow_id: int,
        baseline_ids: set[int],
        *,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        observed: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            observed = [
                item
                for item in self.list_workflow_runs(workflow_id)
                if int(item["id"]) not in baseline_ids
            ]
            comment_runs = [
                item
                for item in observed
                if str(item.get("event") or "") in COMMENT_REVIEW_EVENTS
            ]
            if comment_runs:
                raise ProductionBug(
                    "Agent Review started from a comment event",
                    evidence={
                        "runs": [
                            {
                                "id": item.get("id"),
                                "event": item.get("event"),
                                "html_url": item.get("html_url"),
                                "display_title": item.get("display_title"),
                            }
                            for item in comment_runs
                        ]
                    },
                )
            time.sleep(self.poll_seconds)
        return observed

    def wait_attempt(self, run_id: int, timeout_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            data = self.api(f"repos/{self.repo}/actions/runs/{run_id}")
            if data.get("status") == "completed":
                return data
            time.sleep(self.poll_seconds)
        raise ExternalServiceBlocker(f"workflow run {run_id} did not complete before timeout")

    def attempt_jobs(self, run_id: int, attempt: int) -> dict[str, str]:
        jobs_data = self.api(f"repos/{self.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs")
        return {
            str(item["name"]): str(item.get("conclusion")) for item in jobs_data.get("jobs", [])
        }

    def run_evidence(self, data: dict[str, Any]) -> RunEvidence:
        run_id = int(data["id"])
        attempt = int(data.get("run_attempt", 1))
        jobs = self.attempt_jobs(run_id, attempt)
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

    def wait_new_workflow_runs_settled(
        self,
        workflow_id: int,
        baseline_ids: set[int],
        *,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout_seconds
        latest: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            latest = [
                item
                for item in self.list_workflow_runs(workflow_id)
                if int(item["id"]) not in baseline_ids
            ]
            if latest and all(str(item.get("status") or "") == "completed" for item in latest):
                return latest
            time.sleep(self.poll_seconds)
        return latest

    def attempt_prepare_gate(self, run_id: int, attempt: int) -> dict[str, Any] | None:
        payload, _log_error = self.read_prepare_gate(run_id, attempt)
        return payload

    def read_prepare_gate(self, run_id: int, attempt: int) -> tuple[dict[str, Any] | None, bool]:
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
            return None, True
        found: dict[str, Any] | None = None
        for line in completed.stdout.splitlines():
            brace = line.find("{")
            if brace < 0:
                continue
            try:
                payload = json.loads(line[brace:])
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or "should_review" not in payload:
                continue
            found = {
                "should_review": payload.get("should_review"),
                "reason": payload.get("reason"),
                "head_sha": payload.get("head_sha"),
                "pull_number": payload.get("pull_number"),
            }
        return found, False

    def observe_terminal_transports(
        self,
        *,
        workflow_id: int,
        baseline_ids: set[int],
        heads: list[tuple[str, str]],
        actor: str,
        check_app_slug: str,
        status_context: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        raw_runs = self.wait_new_workflow_runs_settled(
            workflow_id, baseline_ids, timeout_seconds=timeout_seconds
        )
        workflow_runs: list[dict[str, Any]] = []
        for item in raw_runs:
            run_id = int(item["id"])
            attempt = int(item.get("run_attempt") or 1)
            event_name = str(item.get("event") or "")
            status = str(item.get("status") or "")
            jobs: dict[str, str] = {}
            events: list[str] = []
            gate: dict[str, Any] | None = None
            if status == "completed":
                evidence = self.run_evidence(item)
                jobs = dict(evidence.jobs)
                events = list(evidence.events)
                gate = self.attempt_prepare_gate(run_id, attempt)
            workflow_runs.append(
                {
                    "id": run_id,
                    "event": event_name,
                    "status": status,
                    "conclusion": item.get("conclusion"),
                    "html_url": item.get("html_url"),
                    "jobs": jobs,
                    "prepare": gate,
                    "events": events,
                }
            )
        head_rows: list[dict[str, Any]] = []
        kinds: list[str] = []
        for role, sha in heads:
            checks = [
                _compact_check_run(item)
                for item in self.list_check_runs(sha)
                if _check_app_slug(item) == check_app_slug
            ]
            statuses = [
                _compact_commit_status(item)
                for item in self.list_commit_statuses(sha)
                if _status_matches(item, actor=actor, status_context=status_context)
            ]
            terminal = self.coderabbit_terminal(
                sha,
                actor=actor,
                check_app_slug=check_app_slug,
                status_context=status_context,
            )
            kinds.append(str(terminal.get("kind") or ""))
            head_rows.append(
                {
                    "role": role,
                    "head_sha": sha,
                    "check_run_present": bool(checks),
                    "status_present": bool(statuses),
                    "check_runs": checks,
                    "statuses": statuses,
                    "terminal": terminal,
                }
            )
        return {
            "workflow_start_count": len(workflow_runs),
            "check_run_workflow_starts": sum(
                1 for item in workflow_runs if item["event"] == "check_run"
            ),
            "status_workflow_starts": sum(
                1 for item in workflow_runs if item["event"] == "status"
            ),
            "workflow_runs": workflow_runs,
            "heads": head_rows,
            "completed_observed": KIND_COMPLETED in kinds,
            "skipped_observed": KIND_SKIPPED in kinds,
        }

    def head_transport_history(
        self,
        sha: str,
        *,
        actor: str,
        check_app_slug: str,
        status_context: str,
    ) -> dict[str, Any]:
        checks = [
            _compact_check_run(item)
            for item in self.list_check_runs(sha)
            if _check_app_slug(item) == check_app_slug
        ]
        statuses = [
            _compact_commit_status(item)
            for item in self.list_commit_statuses(sha)
            if _status_matches(item, actor=actor, status_context=status_context)
        ]
        return {
            "head_sha": sha,
            "check_runs": checks,
            "commit_statuses": statuses,
        }

    def classify_review_candidate(
        self,
        item: dict[str, Any],
        *,
        pr_number: int,
        head_sha: str,
        cache: dict[int, dict[str, Any]],
    ) -> dict[str, Any]:
        run_id = int(item["id"])
        cached = cache.get(run_id)
        if cached and cached.get("kind") in _STABLE_CANDIDATE_KINDS:
            return {
                **cached,
                "run": item,
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "run_head_sha": item.get("head_sha"),
            }
        attempt = int(item.get("run_attempt") or 1)
        prepare = cached.get("prepare") if cached else None
        log_error = bool(cached.get("prepare_log_error")) if cached else False
        if prepare is None:
            prepare, log_error = self.read_prepare_gate(run_id, attempt)
        jobs = dict(cached.get("jobs") or {}) if cached else {}
        run_status = str(item.get("status") or "")
        if run_status not in {"queued", "requested"}:
            try:
                jobs = self.attempt_jobs(run_id, attempt)
            except (CommandError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                jobs = jobs or {}
        kind = classify_terminal_wake_candidate(
            prepare=prepare,
            jobs=jobs,
            run_status=run_status,
            pr_number=pr_number,
            head_sha=head_sha,
            prepare_log_error=log_error,
        )
        snapshot = {
            "id": run_id,
            "kind": kind,
            "run": item,
            "event": item.get("event"),
            "html_url": item.get("html_url"),
            "status": item.get("status"),
            "conclusion": item.get("conclusion"),
            "run_head_sha": item.get("head_sha"),
            "prepare": prepare,
            "prepare_log_error": log_error,
            "jobs": jobs,
        }
        cache[run_id] = snapshot
        return snapshot

    def scenario_a_timeout_evidence(
        self,
        *,
        pr_number: int,
        head_sha: str,
        pr_head_sha: str,
        actor: str,
        check_app_slug: str,
        status_context: str,
        labels: tuple[str, ...],
        diagnostic: dict[str, str] | None,
        classified: list[dict[str, Any]],
    ) -> dict[str, Any]:
        seen_heads: list[str] = []
        for sha in (head_sha, pr_head_sha):
            if sha and sha not in seen_heads:
                seen_heads.append(sha)
        return {
            "pr_number": pr_number,
            "head_sha": head_sha,
            "pr_head_sha": pr_head_sha,
            "configured_actor": actor,
            "labels": list(labels),
            "coderabbit_terminal": diagnostic,
            "candidate_runs": [candidate_evidence_row(item) for item in classified],
            "prepare_results": [
                item.get("prepare") for item in classified if item.get("prepare") is not None
            ],
            "check_run_status_history": [
                self.head_transport_history(
                    sha,
                    actor=actor,
                    check_app_slug=check_app_slug,
                    status_context=status_context,
                )
                for sha in seen_heads
            ],
        }

    def collect_scenario_a_wait_evidence(
        self,
        *,
        pr_number: int,
        head_sha: str,
        actor: str,
        check_app_slug: str,
        status_context: str,
        workflow_id: int,
        baseline_ids: set[int],
    ) -> dict[str, Any]:
        pr = self.pr_evidence(pr_number)
        diagnostic = self.coderabbit_terminal(
            pr.head_sha,
            actor=actor,
            check_app_slug=check_app_slug,
            status_context=status_context,
        )
        cache: dict[int, dict[str, Any]] = {}
        classified = [
            self.classify_review_candidate(
                item,
                pr_number=pr_number,
                head_sha=head_sha,
                cache=cache,
            )
            for item in new_terminal_wake_runs(self.list_workflow_runs(workflow_id), baseline_ids)
        ]
        return self.scenario_a_timeout_evidence(
            pr_number=pr_number,
            head_sha=head_sha,
            pr_head_sha=pr.head_sha,
            actor=actor,
            check_app_slug=check_app_slug,
            status_context=status_context,
            labels=pr.labels,
            diagnostic=diagnostic,
            classified=classified,
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
        seen_ids: set[int],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        diagnostic: dict[str, str] | None = None
        labels: tuple[str, ...] = ()
        classified: list[dict[str, Any]] = []
        pr_head_sha = head_sha
        cache: dict[int, dict[str, Any]] = {}
        while time.monotonic() < deadline:
            pr = self.pr_evidence(pr_number)
            labels = pr.labels
            pr_head_sha = pr.head_sha
            diagnostic = self.coderabbit_terminal(
                pr.head_sha,
                actor=actor,
                check_app_slug=check_app_slug,
                status_context=status_context,
            )
            candidates = new_terminal_wake_runs(
                self.list_workflow_runs(workflow_id), baseline_ids
            )
            classified = [
                self.classify_review_candidate(
                    item,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    cache=cache,
                )
                for item in candidates
            ]
            for snapshot in classified:
                raise_if_prepare_fault(snapshot)
            review_runs = review_runs_from_classified(classified)
            active = [
                item
                for item in review_runs
                if str(item.get("status") or "") in ACTIVE_RUN_STATUSES
            ]
            unseen_review = choose_unseen_review_run(classified, seen_ids=seen_ids)

            if pr.head_sha == head_sha and not active:
                state = terminal_state(pr)
                latest = completed_review_run_for_terminal(
                    classified,
                    current_head=head_sha,
                    pr_head_sha=pr.head_sha,
                    terminal=state,
                )
                if latest is not None:
                    return {
                        "kind": "production_terminal",
                        "state": state,
                        "run": latest,
                        "coderabbit_terminal": diagnostic,
                    }

            if unseen_review is not None:
                return {
                    "kind": "run",
                    "run": unseen_review,
                    "coderabbit_terminal": diagnostic,
                }

            feedback = [
                item
                for item in self.list_feedback(pr_number)
                if (item.kind, item.source_id, item.updated_at) not in known_ids
                and item.actor == actor
            ]
            if feedback:
                return {
                    "kind": "feedback",
                    "items": feedback,
                    "coderabbit_terminal": diagnostic,
                }
            time.sleep(self.poll_seconds)
        raise ExternalServiceBlocker(
            "production review terminal or review run was not observed before timeout",
            evidence=self.scenario_a_timeout_evidence(
                pr_number=pr_number,
                head_sha=head_sha,
                pr_head_sha=pr_head_sha,
                actor=actor,
                check_app_slug=check_app_slug,
                status_context=status_context,
                labels=labels,
                diagnostic=diagnostic,
                classified=classified,
            ),
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
        supported_events: tuple[str, ...] = (),
        head_sha: str = "",
        generated_file: str = "",
        marker: str,
    ) -> dict[str, Any]:
        del supported_events, head_sha, generated_file
        item = self.api(
            f"repos/{self.repo}/issues/{number}/comments",
            method="POST",
            payload={"body": marker},
        )
        return {"event": "issue_comment", "kind": "issue_comment", "id": item.get("id")}

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
