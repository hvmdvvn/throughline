"""Jira REST v3 client: pagination, 429/Retry-After, retries (issue #11).

Uses recorded fixtures and HTTP mocks — no live Jira calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from throughline.connectors.jira.client import (
    JiraAPIError,
    JiraAuth,
    JiraClient,
    api_v3_base_url,
    build_client_from_tokens,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"
BASE = "https://api.atlassian.com/ex/jira/cloud-fixture/rest/api/3"


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, payload: object, *, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeHTTPError(HTTPError):
    def __init__(
        self,
        *,
        url: str,
        code: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"{}",
    ):
        hdrs = headers or {}
        super().__init__(url, code, f"HTTP Error {code}", hdrs, None)  # type: ignore[arg-type]
        self._body = body

    def read(self) -> bytes:
        return self._body


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client_factory(sleeps: list[float]):
    def factory(*, opener, max_attempts: int = 6, **kwargs: Any) -> JiraClient:
        return JiraClient.from_base_url(
            base_url=BASE,
            access_token="fixture-token",
            opener=opener,
            sleeper=sleeps.append,
            max_attempts=max_attempts,
            backoff_base_seconds=1.0,
            backoff_cap_seconds=60.0,
            **kwargs,
        )

    return factory


def test_api_v3_base_url_uses_cloud_id_not_hardcoded_site():
    url = api_v3_base_url("abc-cloud")
    assert url == "https://api.atlassian.com/ex/jira/abc-cloud/rest/api/3"
    assert "atlassian.net" not in url


def test_build_client_from_tokens_injects_auth():
    client = build_client_from_tokens(
        access_token="tok",
        cloud_id="cid-1",
        opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP")),
        sleeper=lambda _d: None,
    )
    assert client.base_url.endswith("/ex/jira/cid-1/rest/api/3")


def test_explicit_base_url_overrides_site():
    client = JiraClient(
        JiraAuth(access_token="tok", base_url="https://example.test/rest/api/3"),
        opener=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no HTTP")),
        sleeper=lambda _d: None,
    )
    assert client.base_url == "https://example.test/rest/api/3"


def test_get_issue_from_recorded_fixture(client_factory):
    issue = _load("issue_get.json")

    def opener(req, timeout=60):
        _ = timeout
        assert req.get_method() == "GET"
        assert req.get_full_url() == f"{BASE}/issue/ALPHA-1"
        assert req.get_header("Authorization") == "Bearer fixture-token"
        return _FakeResponse(issue)

    client = client_factory(opener=opener)
    assert client.get("/issue/ALPHA-1") == issue


def test_pagination_aggregates_project_search_fixtures(client_factory):
    pages = {
        0: _load("project_search_page1.json"),
        2: _load("project_search_page2.json"),
    }

    def opener(req, timeout=60):
        _ = timeout
        q = _query(req.get_full_url())
        assert urlsplit(req.get_full_url()).path.endswith("/project/search")
        start = int(q["startAt"][0])
        assert int(q["maxResults"][0]) == 2
        return _FakeResponse(pages[start])

    client = client_factory(opener=opener)
    projects = client.get_all("/project/search", page_size=2)
    assert [p["key"] for p in projects] == ["ALPHA", "BETA", "GAMMA"]


def test_pagination_iterator_for_search_issues(client_factory):
    pages = {
        0: _load("search_page1.json"),
        2: _load("search_page2.json"),
    }
    calls: list[int] = []

    def opener(req, timeout=60):
        _ = timeout
        start = int(_query(req.get_full_url())["startAt"][0])
        calls.append(start)
        return _FakeResponse(pages[start])

    client = client_factory(opener=opener)
    keys = [item["key"] for item in client.iter_items("/search", page_size=2, list_key="issues")]
    assert keys == ["ALPHA-1", "ALPHA-2", "ALPHA-3", "ALPHA-4"]
    assert calls == [0, 2]


def test_429_respects_retry_after(client_factory, sleeps: list[float]):
    calls = {"n": 0}

    def opener(req, timeout=60):
        _ = timeout
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeHTTPError(
                url=req.get_full_url(),
                code=429,
                headers={"Retry-After": "7"},
            )
        return _FakeResponse(_load("issue_get.json"))

    client = client_factory(opener=opener)
    assert client.get("/issue/ALPHA-1")["key"] == "ALPHA-1"
    assert sleeps == [7.0]
    assert calls["n"] == 2


def test_429_without_retry_after_uses_exponential_backoff(client_factory, sleeps: list[float]):
    calls = {"n": 0}

    def opener(req, timeout=60):
        _ = timeout
        calls["n"] += 1
        if calls["n"] == 1:
            raise _FakeHTTPError(url=req.get_full_url(), code=429)
        return _FakeResponse({"ok": True})

    client = client_factory(opener=opener)
    assert client.get("/ok") == {"ok": True}
    assert len(sleeps) == 1
    # base * 2^0 = 1.0, plus up to 10% jitter
    assert 1.0 <= sleeps[0] <= 1.1


def test_5xx_retries_then_succeeds(client_factory, sleeps: list[float]):
    calls = {"n": 0}

    def opener(req, timeout=60):
        _ = timeout
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHTTPError(url=req.get_full_url(), code=503)
        return _FakeResponse({"ok": True})

    client = client_factory(opener=opener)
    assert client.get("/ok") == {"ok": True}
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_network_error_retries_with_bound(client_factory, sleeps: list[float]):
    def opener(req, timeout=60):
        _ = req, timeout
        raise URLError("connection reset")

    client = client_factory(opener=opener, max_attempts=3)
    with pytest.raises(JiraAPIError, match="transient"):
        client.get("/ok")
    assert len(sleeps) == 2  # slept between attempts 1-2 and 2-3


def test_permanent_4xx_does_not_retry(client_factory, sleeps: list[float]):
    calls = {"n": 0}

    def opener(req, timeout=60):
        _ = timeout
        calls["n"] += 1
        raise _FakeHTTPError(url=req.get_full_url(), code=404)

    client = client_factory(opener=opener)
    with pytest.raises(JiraAPIError) as exc_info:
        client.get("/missing")
    assert exc_info.value.status_code == 404
    assert exc_info.value.retryable is False
    assert calls["n"] == 1
    assert sleeps == []


def test_exhausted_429_raises(client_factory, sleeps: list[float]):
    def opener(req, timeout=60):
        _ = timeout
        raise _FakeHTTPError(
            url=req.get_full_url(),
            code=429,
            headers={"Retry-After": "1"},
        )

    client = client_factory(opener=opener, max_attempts=2)
    with pytest.raises(JiraAPIError) as exc_info:
        client.get("/ok")
    assert exc_info.value.status_code == 429
    assert len(sleeps) == 1
