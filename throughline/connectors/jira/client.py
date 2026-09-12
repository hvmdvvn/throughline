"""Jira Cloud REST API v3 HTTP client.

All application Jira REST traffic must go through ``JiraClient``. OAuth/token
endpoints stay in ``oauth.py`` (not REST v3). Credentials and base URL are
injected - never hardcoded to a site.
"""

from __future__ import annotations

import contextlib
import json
import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

# Default page size for list endpoints (Jira commonly caps around 50-100).
DEFAULT_PAGE_SIZE = 50
# Bounded retries for 429 / 5xx / transient network failures.
DEFAULT_MAX_ATTEMPTS = 6
# Aggressive exponential backoff base (plan section 5: import speed is not the bottleneck).
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
DEFAULT_BACKOFF_CAP_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 60.0

# Common Jira list payload keys.
_LIST_KEYS = ("values", "issues", "comments", "worklogs", "changelogs")


class JiraAPIError(RuntimeError):
    """Jira REST failure with a safe message (no tokens)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True)
class JiraAuth:
    """Injectable org-scoped credentials for the REST client.

    ``anonymous=True`` omits the Authorization header (public Server sites such
    as ASF Jira). Customer Cloud OAuth always uses a non-empty bearer token.
    """

    access_token: str
    cloud_id: str | None = None
    base_url: str | None = None
    anonymous: bool = False


def api_v3_base_url(cloud_id: str, *, gateway: str = "https://api.atlassian.com") -> str:
    """Build the OAuth-compatible Jira REST v3 base URL for a cloud id."""
    cloud = cloud_id.strip()
    if not cloud:
        raise JiraAPIError("cloud_id is required to build the Jira API base URL")
    root = gateway.rstrip("/")
    return f"{root}/ex/jira/{cloud}/rest/api/3"


def _resolve_base_url(auth: JiraAuth, *, gateway: str = "https://api.atlassian.com") -> str:
    if auth.base_url and auth.base_url.strip():
        return auth.base_url.rstrip("/")
    if auth.cloud_id and auth.cloud_id.strip():
        return api_v3_base_url(auth.cloud_id, gateway=gateway)
    raise JiraAPIError("JiraAuth requires base_url or cloud_id")


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse ``Retry-After`` as delay seconds (integer or HTTP-date)."""
    if header_value is None:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(int(raw))
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        # Treat naive as UTC-ish delta from now; prefer integer form in practice.
        delay = when.timestamp() - time.time()
    else:
        delay = when.timestamp() - time.time()
    return max(0.0, delay)


def _backoff_seconds(attempt: int, *, base: float, cap: float) -> float:
    """Exponential backoff with light jitter; ``attempt`` is 0-based after a failure."""
    exp = min(cap, base * (2**attempt))
    jitter = random.uniform(0.0, exp * 0.1)
    return min(cap, exp + jitter)


def _merge_query(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            continue
        existing[key] = str(value)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment)
    )


def _detect_list_key(payload: Mapping[str, Any], preferred: str | None) -> str | None:
    if preferred and preferred in payload and isinstance(payload[preferred], list):
        return preferred
    for key in _LIST_KEYS:
        if key in payload and isinstance(payload[key], list):
            return key
    return None


def _page_is_last(
    payload: Mapping[str, Any],
    *,
    list_key: str,
    start_at: int,
    max_results: int,
) -> bool:
    if payload.get("isLast") is True:
        return True
    items = payload.get(list_key)
    if not isinstance(items, list):
        return True
    if len(items) == 0:
        return True
    total = payload.get("total")
    if isinstance(total, int):
        return start_at + len(items) >= total
    # No total: stop when a short page is returned.
    return len(items) < max_results


class JiraClient:
    """Thin Jira REST v3 wrapper: retries, rate limits, transparent pagination."""

    def __init__(
        self,
        auth: JiraAuth,
        *,
        gateway: str = "https://api.atlassian.com",
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        sleeper: Callable[[float], None] | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        token = auth.access_token.strip()
        if not auth.anonymous and not token:
            raise JiraAPIError("access_token is required")
        self._anonymous = bool(auth.anonymous)
        self._access_token = token
        self._base_url = _resolve_base_url(auth, gateway=gateway)
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._timeout = timeout_seconds
        self._sleep = sleeper or time.sleep
        self._urlopen = opener or urlopen

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def anonymous(self) -> bool:
        return self._anonymous

    @classmethod
    def from_cloud(
        cls,
        *,
        cloud_id: str,
        access_token: str,
        gateway: str = "https://api.atlassian.com",
        **kwargs: Any,
    ) -> JiraClient:
        """Build a client for an org's cloud id + bearer token."""
        return cls(
            JiraAuth(access_token=access_token, cloud_id=cloud_id),
            gateway=gateway,
            **kwargs,
        )

    @classmethod
    def from_base_url(
        cls,
        *,
        base_url: str,
        access_token: str = "",
        anonymous: bool = False,
        **kwargs: Any,
    ) -> JiraClient:
        """Build a client with an explicit REST base URL (fixture / public site)."""
        return cls(
            JiraAuth(access_token=access_token, base_url=base_url, anonymous=anonymous),
            **kwargs,
        )

    def _build_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = self._base_url.rstrip("/") + "/"
        return urljoin(base, path.lstrip("/"))

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Perform one logical HTTP call with retry / rate-limit handling."""
        url = _merge_query(self._build_url(path), params)
        body: bytes | None = None
        req_headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": "throughline",
        }
        if not self._anonymous:
            req_headers["Authorization"] = f"Bearer {self._access_token}"
        if headers:
            req_headers.update(headers)
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            req_headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(self._max_attempts):
            req = Request(url, data=body, headers=req_headers, method=method.upper())
            try:
                with self._urlopen(req, timeout=self._timeout) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except HTTPError as exc:
                status = exc.code
                headers_map = getattr(exc, "headers", None)
                retry_after = None
                if headers_map is not None:
                    retry_after = headers_map.get("Retry-After")
                # Drain body so connections can reuse; ignore parse failures.
                with contextlib.suppress(OSError):
                    exc.read()

                if status == 429:
                    last_error = JiraAPIError(
                        "Jira rate limited (429)",
                        status_code=429,
                        retryable=True,
                    )
                    if attempt + 1 >= self._max_attempts:
                        break
                    delay = _parse_retry_after(retry_after)
                    if delay is None:
                        delay = _backoff_seconds(
                            attempt,
                            base=self._backoff_base,
                            cap=self._backoff_cap,
                        )
                    self._sleep(delay)
                    continue

                if 500 <= status <= 599:
                    last_error = JiraAPIError(
                        f"Jira server error ({status})",
                        status_code=status,
                        retryable=True,
                    )
                    if attempt + 1 >= self._max_attempts:
                        break
                    self._sleep(
                        _backoff_seconds(
                            attempt,
                            base=self._backoff_base,
                            cap=self._backoff_cap,
                        )
                    )
                    continue

                # Permanent client errors (other than 429): do not retry.
                raise JiraAPIError(
                    f"Jira request failed ({status})",
                    status_code=status,
                    retryable=False,
                ) from exc
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = JiraAPIError(
                    "Jira request failed (transient network or parse error)",
                    retryable=True,
                )
                if attempt + 1 >= self._max_attempts:
                    raise last_error from exc
                self._sleep(
                    _backoff_seconds(
                        attempt,
                        base=self._backoff_base,
                        cap=self._backoff_cap,
                    )
                )
                continue

        assert last_error is not None
        raise last_error

    def get(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.request("POST", path, params=params, json_body=json_body)

    def put(
        self,
        path: str,
        *,
        json_body: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return self.request("PUT", path, params=params, json_body=json_body)

    def delete(self, path: str, *, params: Mapping[str, Any] | None = None) -> Any:
        return self.request("DELETE", path, params=params)

    def iter_pages(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        list_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield raw page payloads, following ``startAt`` / ``maxResults`` pagination."""
        query: dict[str, Any] = dict(params or {})
        start_at = int(query.pop("startAt", 0))
        max_results = int(query.pop("maxResults", page_size))

        while True:
            page_params = {**query, "startAt": start_at, "maxResults": max_results}
            payload = self.get(path, params=page_params)
            if not isinstance(payload, dict):
                raise JiraAPIError("Expected a JSON object page from Jira list endpoint")
            yield payload

            key = _detect_list_key(payload, list_key)
            if key is None:
                # Non-list or already-complete payload.
                return

            items = payload[key]
            assert isinstance(items, Sequence)

            next_page = payload.get("nextPage")
            if isinstance(next_page, str) and next_page.strip():
                # Absolute nextPage URL: follow it as the next path (full URL).
                path = next_page
                # nextPage already encodes startAt; avoid double-querying.
                query = {}
                start_at = 0
                # Keep maxResults from payload if present.
                if isinstance(payload.get("maxResults"), int):
                    max_results = payload["maxResults"]
                continue

            if _page_is_last(payload, list_key=key, start_at=start_at, max_results=max_results):
                return

            start_at = start_at + len(items)

    def iter_items(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        list_key: str | None = None,
    ) -> Iterator[Any]:
        """Yield individual list items across all pages (no manual page loops)."""
        for page in self.iter_pages(
            path, params=params, page_size=page_size, list_key=list_key
        ):
            key = _detect_list_key(page, list_key)
            if key is None:
                return
            items = page[key]
            if not isinstance(items, list):
                return
            yield from items

    def get_all(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        list_key: str | None = None,
    ) -> list[Any]:
        """Return all items from a paginated list endpoint as one aggregated list."""
        return list(
            self.iter_items(path, params=params, page_size=page_size, list_key=list_key)
        )


def build_client_from_tokens(
    *,
    access_token: str,
    cloud_id: str | None = None,
    base_url: str | None = None,
    **kwargs: Any,
) -> JiraClient:
    """Factory used by the connection layer — injectable auth, no hardcoded site."""
    return JiraClient(
        JiraAuth(access_token=access_token, cloud_id=cloud_id, base_url=base_url),
        **kwargs,
    )
