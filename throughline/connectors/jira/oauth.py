"""Atlassian OAuth 2.0 (3LO) HTTP helpers for Jira Cloud.

Authorization and token endpoints are mocked in tests — no live Atlassian calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from throughline.config import settings

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# Classic Jira Cloud scopes + offline_access for refresh tokens (plan §5).
DEFAULT_SCOPES = "read:jira-work read:jira-user offline_access"


class AtlassianOAuthError(RuntimeError):
    """Atlassian OAuth HTTP or protocol failure (safe message, no tokens)."""


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str | None = None
    token_type: str | None = None


@dataclass(frozen=True)
class AccessibleResource:
    cloud_id: str
    url: str
    name: str
    scopes: list[str]


def build_authorize_url(*, state: str, scopes: str | None = None) -> str:
    """Build the Atlassian 3LO authorization URL (includes ``offline_access``)."""
    client_id = settings.atlassian_client_id.strip()
    redirect_uri = settings.atlassian_redirect_uri.strip()
    if not client_id or not redirect_uri:
        raise AtlassianOAuthError(
            "Atlassian OAuth is not configured (set ATLASSIAN_CLIENT_ID and "
            "ATLASSIAN_REDIRECT_URI)"
        )
    scope = (scopes or settings.atlassian_oauth_scopes or DEFAULT_SCOPES).strip()
    if "offline_access" not in scope.split():
        scope = f"{scope} offline_access".strip()
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": client_id,
            "scope": scope,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "throughline",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = "Atlassian token request failed"
        try:
            err_body = exc.read().decode("utf-8")
            parsed = json.loads(err_body)
            # Prefer protocol error codes; never echo tokens.
            err = parsed.get("error") or parsed.get("error_description")
            if isinstance(err, str) and err:
                detail = f"Atlassian token request failed: {err}"
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
        raise AtlassianOAuthError(detail) from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise AtlassianOAuthError("Atlassian token request failed") from exc


def _get_json(url: str, *, bearer_token: str) -> Any:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
            "User-Agent": "throughline",
        },
        method="GET",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        raise AtlassianOAuthError("Atlassian accessible-resources request failed") from exc
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise AtlassianOAuthError("Atlassian accessible-resources request failed") from exc


def _parse_token_response(data: dict[str, Any]) -> TokenResponse:
    access = data.get("access_token")
    refresh = data.get("refresh_token")
    expires_in = data.get("expires_in")
    if not isinstance(access, str) or not access:
        raise AtlassianOAuthError("Atlassian token response missing access_token")
    if not isinstance(refresh, str) or not refresh:
        raise AtlassianOAuthError(
            "Atlassian token response missing refresh_token "
            "(ensure offline_access was requested)"
        )
    if not isinstance(expires_in, int) or expires_in <= 0:
        raise AtlassianOAuthError("Atlassian token response missing expires_in")
    scope = data.get("scope")
    token_type = data.get("token_type")
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in=expires_in,
        scope=scope if isinstance(scope, str) else None,
        token_type=token_type if isinstance(token_type, str) else None,
    )


def exchange_authorization_code(code: str) -> TokenResponse:
    """Exchange an authorization code for access + refresh tokens."""
    client_id = settings.atlassian_client_id.strip()
    client_secret = settings.atlassian_client_secret.strip()
    redirect_uri = settings.atlassian_redirect_uri.strip()
    if not client_id or not client_secret or not redirect_uri:
        raise AtlassianOAuthError("Atlassian OAuth client credentials are not configured")
    data = _post_json(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        },
    )
    return _parse_token_response(data)


def refresh_access_token(refresh_token: str) -> TokenResponse:
    """Exchange a refresh token for a new access + refresh token pair (rotating)."""
    client_id = settings.atlassian_client_id.strip()
    client_secret = settings.atlassian_client_secret.strip()
    if not client_id or not client_secret:
        raise AtlassianOAuthError("Atlassian OAuth client credentials are not configured")
    data = _post_json(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
        },
    )
    return _parse_token_response(data)


def fetch_accessible_resources(access_token: str) -> list[AccessibleResource]:
    """List Jira Cloud sites the access token can reach."""
    payload = _get_json(ACCESSIBLE_RESOURCES_URL, bearer_token=access_token)
    if not isinstance(payload, list):
        raise AtlassianOAuthError("Unexpected accessible-resources response")
    resources: list[AccessibleResource] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        cloud_id = item.get("id")
        url = item.get("url")
        name = item.get("name")
        if not isinstance(cloud_id, str) or not cloud_id:
            continue
        if not isinstance(url, str) or not url:
            continue
        scopes = item.get("scopes") or []
        if not isinstance(scopes, list):
            scopes = []
        resources.append(
            AccessibleResource(
                cloud_id=cloud_id,
                url=url,
                name=name if isinstance(name, str) else url,
                scopes=[s for s in scopes if isinstance(s, str)],
            )
        )
    return resources
