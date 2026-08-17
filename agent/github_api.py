"""GitHub REST client for Orchestrator write operations.

Official references (verified 2026-08-17):
- Pulls: https://docs.github.com/en/rest/pulls/pulls
- Labels: https://docs.github.com/en/rest/issues/labels
- Issue comments: https://docs.github.com/en/rest/issues/comments
- Create issue: https://docs.github.com/en/rest/issues/issues
- Headers: Accept application/vnd.github+json, Authorization Bearer,
  X-GitHub-Api-Version 2026-03-10
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from agent.errors import AgentError

API_VERSION = "2026-03-10"
ACCEPT = "application/vnd.github+json"
USER_AGENT = "md-agent-orchestrator"
DEFAULT_TIMEOUT_SECONDS = 30

Requester = Callable[[str, str, dict[str, str], bytes | None], tuple[int, Any]]


@dataclass(frozen=True)
class GitHubResponse:
    status: int
    payload: Any


class GitHubClient:
    """Minimal REST client. Token is used only as an Authorization header."""

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        api_url: str = "https://api.github.com",
        requester: Requester | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not token.strip():
            raise AgentError.environment_failure(
                "GITHUB_TOKEN is required for GitHub write operations",
                code="MISSING_GITHUB_TOKEN",
            )
        if "/" not in repository or repository.count("/") != 1:
            raise AgentError.invalid_input(f"invalid GITHUB_REPOSITORY: {repository!r}")
        self.token = token
        self.owner, self.repo = repository.split("/", 1)
        self.api_url = api_url.rstrip("/")
        self._requester = requester or _default_requester
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> GitHubResponse:
        url = self.api_url + path
        if query:
            url += "?" + urlencode(query)
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Accept": ACCEPT,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, payload = self._requester(method, url, headers, encoded)
        except TimeoutError as exc:
            raise AgentError.environment_failure(
                "GitHub API request timed out",
                code="GITHUB_API_TIMEOUT",
            ) from exc
        except URLError as exc:
            raise AgentError.environment_failure(
                f"GitHub API network error: {exc.reason}",
                code="GITHUB_API_NETWORK",
            ) from exc
        except HTTPError as exc:
            detail = _read_http_error(exc)
            raise _github_http_error(exc.code, detail) from exc
        if status >= 500:
            raise AgentError.environment_failure(
                f"GitHub API {status}",
                code="GITHUB_API_FAILURE",
            )
        if status >= 400:
            raise _github_http_error(status, payload)
        return GitHubResponse(status=status, payload=payload)

    def list_open_pulls(self, *, head_branch: str) -> list[dict[str, Any]]:
        head = f"{self.owner}:{head_branch}"
        response = self.request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/pulls",
            query={"state": "open", "head": head, "per_page": "100"},
        )
        if not isinstance(response.payload, list):
            raise AgentError.environment_failure(
                "GitHub pulls list is not an array",
                code="GITHUB_API_FAILURE",
            )
        return [item for item in response.payload if isinstance(item, dict)]

    def create_pull(self, *, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            body={"title": title, "head": head, "base": base, "body": body},
        )
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub create pull response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload

    def get_label(self, name: str) -> dict[str, Any] | None:
        encoded = quote(name, safe="")
        try:
            response = self.request("GET", f"/repos/{self.owner}/{self.repo}/labels/{encoded}")
        except AgentError as exc:
            if exc.code == "GITHUB_NOT_FOUND":
                return None
            raise
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub label response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload

    def create_label(self, *, name: str, color: str, description: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/labels",
            body={"name": name, "color": color, "description": description},
        )
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub create label response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload

    def add_issue_labels(self, issue_number: int, labels: list[str]) -> None:
        self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels",
            body={"labels": labels},
        )

    def create_issue(
        self, *, title: str, body: str, labels: list[str] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues",
            body=payload,
        )
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub create issue response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload

    def create_issue_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        response = self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
            body={"body": body},
        )
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub comment response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload


def github_client_from_env(
    env: Mapping[str, str] | None = None,
    *,
    requester: Requester | None = None,
) -> GitHubClient:
    source = os.environ if env is None else env
    token = (source.get("GITHUB_TOKEN") or source.get("GH_TOKEN") or "").strip()
    repository = (source.get("GITHUB_REPOSITORY") or "").strip()
    api_url = (source.get("GITHUB_API_URL") or "https://api.github.com").strip()
    if not repository:
        raise AgentError.environment_failure(
            "GITHUB_REPOSITORY is required for GitHub write operations",
            code="MISSING_GITHUB_REPOSITORY",
        )
    return GitHubClient(
        token=token,
        repository=repository,
        api_url=api_url,
        requester=requester,
    )


def _default_requester(
    method: str, url: str, headers: dict[str, str], data: bytes | None
) -> tuple[int, Any]:
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        raw = response.read()
        status = int(getattr(response, "status", 200))
        if not raw:
            return status, None
        return status, json.loads(raw.decode("utf-8"))


def _read_http_error(exc: HTTPError) -> Any:
    raw = exc.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")


def _github_http_error(status: int, payload: Any) -> AgentError:
    message = _payload_message(payload)
    if status in {401, 403}:
        return AgentError.environment_failure(
            f"GitHub API authentication/permission error {status}: {message}",
            code="GITHUB_API_PERMISSION",
        )
    if status == 404:
        return AgentError.environment_failure(
            f"GitHub API not found: {message}",
            code="GITHUB_NOT_FOUND",
        )
    if status == 422:
        return AgentError.environment_failure(
            f"GitHub API validation failed: {message}",
            code="GITHUB_API_VALIDATION",
        )
    if status >= 500:
        return AgentError.environment_failure(
            f"GitHub API {status}: {message}",
            code="GITHUB_API_FAILURE",
        )
    return AgentError.environment_failure(
        f"GitHub API {status}: {message}",
        code="GITHUB_API_FAILURE",
    )


def _payload_message(payload: Any) -> str:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message
    if isinstance(payload, str) and payload.strip():
        return payload
    return "unknown error"
