"""GitHub REST client for Orchestrator write operations.

Official references (verified 2026-08-17 / 2026-08-19):
- Pulls: https://docs.github.com/en/rest/pulls/pulls
- Reviews: https://docs.github.com/en/rest/pulls/reviews
- Review comments: https://docs.github.com/en/rest/pulls/comments
- Labels: https://docs.github.com/en/rest/issues/labels
- Issue comments: https://docs.github.com/en/rest/issues/comments
- Create issue: https://docs.github.com/en/rest/issues/issues
- Contents: https://docs.github.com/en/rest/repos/contents
- Check runs: https://docs.github.com/en/rest/checks/runs
- Commit statuses: https://docs.github.com/en/rest/commits/statuses
- Commit pull requests: https://docs.github.com/en/rest/commits/commits#list-pull-requests-associated-with-a-commit
- Headers: Accept application/vnd.github+json, Authorization Bearer,
  X-GitHub-Api-Version 2026-03-10
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from agent.errors import AgentError

API_VERSION = "2026-03-10"
ACCEPT = "application/vnd.github+json"
USER_AGENT = "md-agent-orchestrator"
DEFAULT_TIMEOUT_SECONDS = 30
PULL_CREATE_TOKEN_ENV = "AGENT_PR_PAT"

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
        pull_create_token: str = "",
    ) -> None:
        if not token.strip():
            raise AgentError.environment_failure(
                "GITHUB_TOKEN is required for GitHub write operations",
                code="MISSING_GITHUB_TOKEN",
            )
        if "/" not in repository or repository.count("/") != 1:
            raise AgentError.invalid_input(f"invalid GITHUB_REPOSITORY: {repository!r}")
        self.token = token
        self.pull_create_token = pull_create_token
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
        authorization_token: str | None = None,
    ) -> GitHubResponse:
        url = self.api_url + path
        if query:
            url += "?" + urlencode(query)
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        bearer = self.token if authorization_token is None else authorization_token
        headers = {
            "Accept": ACCEPT,
            "Authorization": f"Bearer {bearer}",
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
        except HTTPError as exc:
            # HTTPError subclasses URLError. Classify 401/403/404/422 as HTTP
            # errors; GITHUB_API_NETWORK is only for DNS/connect failures.
            detail = _read_http_error(exc)
            raise _github_http_error(exc.code, detail, endpoint=f"{method} {path}") from exc
        except URLError as exc:
            raise AgentError.environment_failure(
                f"GitHub API network error: {exc.reason}",
                code="GITHUB_API_NETWORK",
            ) from exc
        if status >= 500:
            raise AgentError.environment_failure(
                f"GitHub API {status}",
                code="GITHUB_API_FAILURE",
            )
        if status >= 400:
            raise _github_http_error(status, payload, endpoint=f"{method} {path}")
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
        token = self.pull_create_token.strip()
        if not token:
            raise AgentError.environment_failure(
                "AGENT_PR_PAT is required to create pull requests",
                code="MISSING_AGENT_PR_PAT",
            )
        response = self.request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            body={"title": title, "head": head, "base": base, "body": body},
            authorization_token=token,
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
        # Concurrent creates of the same label can return 422 "already exists".
        # Treat that as success in a later hardening change; fail closed for now.
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

    def get_pull(self, number: int) -> dict[str, Any]:
        response = self.request("GET", f"/repos/{self.owner}/{self.repo}/pulls/{number}")
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub pull response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload

    def get_content(self, path: str, *, ref: str) -> str:
        """Return the UTF-8 text of a file at an exact git ref (commit SHA or branch)."""
        relative = _safe_repo_path(path)
        commit = _require_git_ref(ref)
        response = self.request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{quote(relative, safe='/')}",
            query={"ref": commit},
        )
        payload = response.payload
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise AgentError.environment_failure(
                f"GitHub content is not a file: {relative}",
                code="GITHUB_API_FAILURE",
            )
        encoding = payload.get("encoding")
        content = payload.get("content")
        if encoding != "base64" or not isinstance(content, str):
            raise AgentError.environment_failure(
                f"GitHub file content is not base64: {relative}",
                code="GITHUB_API_FAILURE",
            )
        try:
            raw = base64.b64decode(content.encode("ascii"), validate=False)
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise AgentError.environment_failure(
                f"GitHub file content could not be decoded: {relative}",
                code="GITHUB_API_FAILURE",
            ) from exc

    def list_contents(self, path: str, *, ref: str) -> list[dict[str, Any]]:
        """Return immediate children of a directory at an exact git ref."""
        relative = _safe_repo_path(path)
        commit = _require_git_ref(ref)
        response = self.request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/contents/{quote(relative, safe='/')}",
            query={"ref": commit},
        )
        payload = response.payload
        if not isinstance(payload, list):
            raise AgentError.environment_failure(
                f"GitHub content is not a directory: {relative}",
                code="GITHUB_API_FAILURE",
            )
        entries = [item for item in payload if isinstance(item, dict)]
        if len(entries) != len(payload):
            raise AgentError.environment_failure(
                f"GitHub directory listing is malformed: {relative}",
                code="GITHUB_API_FAILURE",
            )
        return entries

    def list_check_runs_for_ref(self, ref: str) -> list[dict[str, Any]]:
        commit = quote(_require_git_ref(ref), safe="")
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 50:
            response = self.request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/commits/{commit}/check-runs",
                query={"per_page": "100", "page": str(page)},
            )
            payload = response.payload
            if not isinstance(payload, dict):
                raise AgentError.environment_failure(
                    "GitHub check-runs response is not an object",
                    code="GITHUB_API_FAILURE",
                )
            batch = payload.get("check_runs")
            if not isinstance(batch, list):
                raise AgentError.environment_failure(
                    "GitHub check-runs list is missing",
                    code="GITHUB_API_FAILURE",
                )
            items.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < 100:
                return items
            page += 1
        raise AgentError.environment_failure(
            "GitHub check-run pagination exceeded the fail-closed page limit",
            code="GITHUB_API_FAILURE",
        )

    def list_commit_statuses_for_ref(self, ref: str) -> list[dict[str, Any]]:
        commit = quote(_require_git_ref(ref), safe="")
        return self._list_paginated(f"/repos/{self.owner}/{self.repo}/commits/{commit}/statuses")

    def list_pulls_for_commit(self, sha: str) -> list[dict[str, Any]]:
        commit = quote(_require_git_ref(sha), safe="")
        return self._list_paginated(f"/repos/{self.owner}/{self.repo}/commits/{commit}/pulls")

    def list_reviews(self, pull_number: int) -> list[dict[str, Any]]:
        return self._list_paginated(f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}/reviews")

    def list_review_comments(self, pull_number: int) -> list[dict[str, Any]]:
        return self._list_paginated(f"/repos/{self.owner}/{self.repo}/pulls/{pull_number}/comments")

    def list_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self._list_paginated(
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments"
        )

    def list_issue_labels(self, issue_number: int) -> list[dict[str, Any]]:
        return self._list_paginated(f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels")

    def update_issue_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        response = self.request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}",
            body={"body": body},
        )
        if not isinstance(response.payload, dict):
            raise AgentError.environment_failure(
                "GitHub comment update response is not an object",
                code="GITHUB_API_FAILURE",
            )
        return response.payload

    def remove_issue_label(self, issue_number: int, name: str) -> None:
        encoded = quote(name, safe="")
        try:
            self.request(
                "DELETE",
                f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels/{encoded}",
            )
        except AgentError as exc:
            if exc.code == "GITHUB_NOT_FOUND":
                return
            raise

    def _list_paginated(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        while page <= 50:
            response = self.request(
                "GET",
                path,
                query={"per_page": "100", "page": str(page)},
            )
            if not isinstance(response.payload, list):
                raise AgentError.environment_failure(
                    "GitHub list response is not an array",
                    code="GITHUB_API_FAILURE",
                )
            batch = [item for item in response.payload if isinstance(item, dict)]
            items.extend(batch)
            if len(response.payload) < 100:
                return items
            page += 1
        raise AgentError.environment_failure(
            "GitHub list pagination exceeded the fail-closed page limit",
            code="GITHUB_API_FAILURE",
        )


def github_client_from_env(
    env: Mapping[str, str] | None = None,
    *,
    requester: Requester | None = None,
) -> GitHubClient:
    source = os.environ if env is None else env
    token = (source.get("GITHUB_TOKEN") or source.get("GH_TOKEN") or "").strip()
    pull_create_token = (source.get(PULL_CREATE_TOKEN_ENV) or "").strip()
    repository = (source.get("GITHUB_REPOSITORY") or "").strip()
    api_url = (source.get("GITHUB_API_URL") or "https://api.github.com").strip()
    if not repository:
        raise AgentError.environment_failure(
            "GITHUB_REPOSITORY is required for GitHub write operations",
            code="MISSING_GITHUB_REPOSITORY",
        )
    return GitHubClient(
        token=token,
        pull_create_token=pull_create_token,
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


def _github_http_error(status: int, payload: Any, *, endpoint: str = "") -> AgentError:
    message = _payload_message(payload)
    where = f" ({endpoint})" if endpoint else ""
    if status in {401, 403}:
        return AgentError.environment_failure(
            f"GitHub API authentication/permission error {status}: {message}{where}",
            code="GITHUB_API_PERMISSION",
        )
    if status == 404:
        return AgentError.environment_failure(
            f"GitHub API not found: {message}{where}",
            code="GITHUB_NOT_FOUND",
        )
    if status == 422:
        return AgentError.environment_failure(
            f"GitHub API validation failed: {message}{where}",
            code="GITHUB_API_VALIDATION",
        )
    if status >= 500:
        return AgentError.environment_failure(
            f"GitHub API {status}: {message}{where}",
            code="GITHUB_API_FAILURE",
        )
    return AgentError.environment_failure(
        f"GitHub API {status}: {message}{where}",
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


def _require_git_ref(ref: str) -> str:
    value = ref.strip()
    if not value:
        raise AgentError.invalid_input("git ref is required")
    return value


def _safe_repo_path(path: str) -> str:
    relative = path.replace("\\", "/").strip("/")
    if not relative or relative.startswith("/") or Path(relative).is_absolute():
        raise AgentError.invalid_input(f"invalid repository content path: {path!r}")
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise AgentError.invalid_input(f"invalid repository content path: {path!r}")
    return relative
