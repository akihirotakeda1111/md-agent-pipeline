from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import unquote

import pytest
from agent.errors import AgentError, ErrorCategory
from agent.github_api import PULL_CREATE_TOKEN_ENV, GitHubClient, Requester, github_client_from_env
from agent.labels import PHASE6_APPLIED_LABELS, ensure_agent_labels


def _http_error(url: str, status: int, message: str) -> HTTPError:
    body = json.dumps({"message": message}).encode("utf-8")
    return HTTPError(url, status, message, hdrs={}, fp=BytesIO(body))


def _client(requester: Requester) -> GitHubClient:
    return GitHubClient(token="tok", repository="octo/repo", requester=requester)


@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (401, "Bad credentials", "GITHUB_API_PERMISSION"),
        (403, "Resource not accessible", "GITHUB_API_PERMISSION"),
        (404, "Not Found", "GITHUB_NOT_FOUND"),
        (422, "Validation Failed", "GITHUB_API_VALIDATION"),
    ],
)
def test_http_error_is_classified_not_network(status: int, message: str, code: str) -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        raise _http_error(url, status, message)

    with pytest.raises(AgentError) as caught:
        _client(requester).request("GET", "/repos/octo/repo/commits/abc/check-runs")
    assert caught.value.code == code
    assert caught.value.code != "GITHUB_API_NETWORK"
    assert message in str(caught.value)
    assert "GET /repos/octo/repo/commits/abc/check-runs" in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        URLError("getaddrinfo failed"),
        URLError(ConnectionRefusedError("Connection refused")),
    ],
)
def test_urlerror_is_network_failure(error: URLError) -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        raise error

    with pytest.raises(AgentError) as caught:
        _client(requester).request("GET", "/repos/octo/repo/labels/agent%3Aready")
    assert caught.value.code == "GITHUB_API_NETWORK"


def test_get_label_treats_http_404_as_missing() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        raise _http_error(url, 404, "Not Found")

    assert _client(requester).get_label("agent:ready") is None


def test_ensure_agent_labels_creates_after_get_404() -> None:
    created: list[str] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        if method == "GET":
            raise _http_error(url, 404, "Not Found")
        assert method == "POST"
        payload = json.loads(data or b"{}")
        created.append(payload["name"])
        return 201, payload

    ensure_agent_labels(_client(requester))
    assert created == list(PHASE6_APPLIED_LABELS)


def test_get_label_does_not_swallow_permission_errors() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        raise _http_error(url, 401, "Bad credentials")

    with pytest.raises(AgentError) as caught:
        _client(requester).get_label("agent:ready")
    assert caught.value.code == "GITHUB_API_PERMISSION"


def test_label_get_encodes_colon() -> None:
    urls: list[str] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        urls.append(url)
        raise _http_error(url, 404, "Not Found")

    assert _client(requester).get_label("agent:ready") is None
    assert urls[0].endswith("/labels/agent%3Aready")
    assert unquote(urls[0].rsplit("/", 1)[-1]) == "agent:ready"


def test_get_content_decodes_base64_at_ref() -> None:
    urls: list[str] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        urls.append(url)
        return 200, {
            "type": "file",
            "encoding": "base64",
            "content": "LS0tCmlkOiBkZW1vCi0tLQo=",
        }

    text = _client(requester).get_content("specs/tasks/demo.md", ref="abc123")
    assert text.startswith("---")
    assert "/contents/specs/tasks/demo.md" in urls[0]
    assert "ref=abc123" in urls[0]


def test_get_content_rejects_directory_payload() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        return 200, [{"type": "file", "path": "specs/tasks/demo.md", "name": "demo.md"}]

    with pytest.raises(AgentError) as caught:
        _client(requester).get_content("specs/tasks", ref="abc123")
    assert caught.value.code == "GITHUB_API_FAILURE"


def test_get_content_rejects_empty_ref() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        raise AssertionError("must not call GitHub without a ref")

    with pytest.raises(AgentError) as caught:
        _client(requester).get_content("specs/tasks/demo.md", ref="  ")
    assert caught.value.category is ErrorCategory.INVALID_INPUT


def test_list_contents_returns_directory_entries() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        return 200, [
            {"type": "file", "path": "specs/tasks/demo.md", "name": "demo.md"},
            {"type": "dir", "path": "specs/tasks/nested", "name": "nested"},
        ]

    entries = _client(requester).list_contents("specs/tasks", ref="abc123")
    assert [item["name"] for item in entries] == ["demo.md", "nested"]


def test_list_check_runs_for_ref_reads_check_runs_array() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        assert "/commits/abc123/check-runs" in url
        return 200, {
            "total_count": 1,
            "check_runs": [{"id": 1, "head_sha": "abc123", "conclusion": "success"}],
        }

    runs = _client(requester).list_check_runs_for_ref("abc123")
    assert runs[0]["conclusion"] == "success"


def test_list_commit_statuses_and_pulls_for_commit() -> None:
    urls: list[str] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        urls.append(url)
        if url.endswith("/statuses") or "/statuses?" in url:
            return 200, [{"state": "success", "context": "CodeRabbit"}]
        return 200, [{"number": 7, "state": "open"}]

    client = _client(requester)
    assert client.list_commit_statuses_for_ref("abc123")[0]["context"] == "CodeRabbit"
    assert client.list_pulls_for_commit("abc123")[0]["number"] == 7
    assert any("/commits/abc123/statuses" in url for url in urls)
    assert any("/commits/abc123/pulls" in url for url in urls)


def test_create_pull_uses_agent_pr_pat_not_github_token() -> None:
    auths: list[tuple[str, str, str]] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        auths.append((method, url, headers["Authorization"]))
        if method == "GET":
            return 200, []
        return 201, {"number": 3, "html_url": "https://example.test/pull/3"}

    client = GitHubClient(
        token="github-token",
        pull_create_token="pr-pat",
        repository="octo/repo",
        requester=requester,
    )
    created = client.create_pull(title="t", head="feature", base="main", body="body")
    listed = client.list_open_pulls(head_branch="feature")
    assert created["number"] == 3
    assert listed == []
    assert auths[0] == ("POST", "https://api.github.com/repos/octo/repo/pulls", "Bearer pr-pat")
    assert auths[1][0] == "GET"
    assert auths[1][2] == "Bearer github-token"


def test_create_pull_fails_closed_without_agent_pr_pat() -> None:
    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        raise AssertionError("must not call GitHub without AGENT_PR_PAT")

    client = GitHubClient(token="github-token", repository="octo/repo", requester=requester)
    with pytest.raises(AgentError) as caught:
        client.create_pull(title="t", head="feature", base="main", body="body")
    assert caught.value.code == "MISSING_AGENT_PR_PAT"


def test_github_client_from_env_reads_agent_pr_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def requester(
        method: str, url: str, headers: dict[str, str], data: bytes | None
    ) -> tuple[int, object]:
        seen.append(headers["Authorization"])
        return 201, {"number": 8, "html_url": "https://example.test/pull/8"}

    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv(PULL_CREATE_TOKEN_ENV, "env-pr-pat")
    monkeypatch.setenv("GITHUB_REPOSITORY", "octo/repo")
    client = github_client_from_env(requester=requester)
    client.create_pull(title="t", head="feature", base="main", body="body")
    assert seen == ["Bearer env-pr-pat"]
    assert client.token == "github-token"
