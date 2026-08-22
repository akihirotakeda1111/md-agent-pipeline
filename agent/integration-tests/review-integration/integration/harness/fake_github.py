from __future__ import annotations

from copy import deepcopy
from typing import Any

from .observations import ObservationLog


class FakeGitHub:
    """Stateful GitHub transport. GET returns current state; only writes mutate.

    Scripted `responses` seed the live objects. Tests may update
    `responses[operation][0]` in place; later GETs see that state. Queued
    extra list entries are ignored so a run cannot accidentally pop a
    different snapshot.
    """

    def __init__(
        self, observations: ObservationLog, responses: dict[str, list[Any]] | None = None
    ) -> None:
        self.observations = observations
        self.responses = {key: list(values) for key, values in (responses or {}).items()}
        self.tracking_body: str | None = None
        self.tracking_id: int | None = None
        self.issue_comments: list[dict[str, Any]] = []
        self.labels: set[str] = set()
        self.issue_labels: set[str] = set()
        self.next_id = 8000
        self.failures: dict[str, BaseException] = {}

    def fail(self, operation: str, error: BaseException) -> None:
        self.failures[operation] = error

    def current(self, operation: str, default: Any = None) -> Any:
        items = self.responses.setdefault(operation, [])
        if not items:
            items.append([] if default is None else deepcopy(default))
        return items[0]

    def request(self, operation: str, **payload: Any) -> Any:
        self.observations.github_call(operation, payload)
        error = self.failures.get(operation)
        if error is not None:
            raise error
        return deepcopy(self.current(operation))

    def calls(self, operation: str) -> list[dict[str, Any]]:
        return [item.payload for item in self.observations.github if item.operation == operation]

    def get_pull_request(self, **payload: Any) -> Any:
        return self.request("get_pull_request", **payload)

    def list_review_feedback(self, **payload: Any) -> Any:
        return self.request("list_review_feedback", **payload)

    def load_processed_reviews(self, **payload: Any) -> Any:
        return self.request("load_processed_reviews", **payload)

    def save_processed_reviews(self, **payload: Any) -> Any:
        self.observations.github_call("save_processed_reviews", payload)
        records = payload.get("records")
        if records is not None:
            live = self.current("load_processed_reviews", [])
            if isinstance(live, list):
                live.clear()
                live.extend(deepcopy(records))
        body = payload.get("body")
        if isinstance(body, str):
            self.tracking_body = body
            if self.tracking_id is None:
                self.tracking_id = self.next_id
                self.next_id += 1
        existing = self.responses.get("save_processed_reviews")
        if existing:
            return deepcopy(existing[0])
        return {"id": self.tracking_id or self.next_id, "body": self.tracking_body}

    def add_pr_comment(self, **payload: Any) -> Any:
        self.observations.github_call("add_pr_comment", payload)
        error = self.failures.get("add_pr_comment")
        if error is not None:
            raise error
        comment = {
            "id": self.next_id,
            "body": payload.get("body"),
            "user": payload.get("user") or {"login": "github-actions[bot]"},
        }
        self.next_id += 1
        self.issue_comments.append(comment)
        return deepcopy(comment)

    def set_labels(self, **payload: Any) -> Any:
        self.observations.github_call("set_labels", payload)
        error = self.failures.get("set_labels")
        if error is not None:
            raise error
        for name in payload.get("labels") or ():
            self.labels.add(str(name))
        if payload.get("name"):
            self.labels.add(str(payload["name"]))
        return {"labels": sorted(self.labels)}
