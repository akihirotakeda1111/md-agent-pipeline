from __future__ import annotations

from copy import deepcopy
from typing import Any

from .observations import ObservationLog


class FakeGitHub:
    """Records API requests and returns configured values without making decisions."""

    def __init__(self, observations: ObservationLog, responses: dict[str, list[Any]] | None = None) -> None:
        self.observations = observations
        self.responses = {name: list(values) for name, values in (responses or {}).items()}

    def request(self, operation: str, **payload: Any) -> Any:
        self.observations.github_call(operation, payload)
        queue = self.responses.get(operation, [])
        return deepcopy(queue.pop(0)) if queue else None

    def calls(self, operation: str) -> list[dict[str, Any]]:
        return [item.payload for item in self.observations.github if item.operation == operation]

    # Explicit methods keep production adapters small and requests inspectable.
    def list_pull_requests(self, **payload: Any) -> Any:
        return self.request("list_pull_requests", **payload)

    def create_pull_request(self, **payload: Any) -> Any:
        return self.request("create_pull_request", **payload)

    def ensure_label(self, **payload: Any) -> Any:
        return self.request("ensure_label", **payload)

    def set_labels(self, **payload: Any) -> Any:
        return self.request("set_labels", **payload)

    def add_pr_comment(self, **payload: Any) -> Any:
        return self.request("add_pr_comment", **payload)

    def create_issue(self, **payload: Any) -> Any:
        return self.request("create_issue", **payload)
