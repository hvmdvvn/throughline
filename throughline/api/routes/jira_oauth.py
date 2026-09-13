"""Jira Cloud OAuth 2.0 (3LO) admin + callback routes (issue #10).

Authorize / status / disconnect require Clerk auth and resolve the current org
from membership. The Atlassian callback is unauthenticated; org is bound via
encrypted OAuth ``state``.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import JiraConnectionStatusResponse, JiraOAuthAuthorizeResponse
from throughline.config import settings
from throughline.connectors.jira.oauth import AtlassianOAuthError
from throughline.connectors.jira.service import (
    authorization_redirect_url,
    complete_oauth_callback,
    connection_status,
    disconnect_jira,
    get_active_connection,
)
from throughline.tenancy import use_org

router = APIRouter(tags=["jira-oauth"])

admin_router = APIRouter(
    prefix="/admin/jira",
    tags=["jira-oauth"],
    dependencies=[Depends(require_admin)],
)


@admin_router.get("/oauth/authorize", response_model=JiraOAuthAuthorizeResponse)
def jira_oauth_authorize(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    redirect: Annotated[bool, Query(description="302 redirect to Atlassian")] = False,
) -> JiraOAuthAuthorizeResponse | RedirectResponse:
    """Start Atlassian 3LO for the current org (requests ``offline_access``)."""
    try:
        url = authorization_redirect_url(org_id)
    except AtlassianOAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    if redirect:
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)
    return JiraOAuthAuthorizeResponse(authorize_url=url)


@admin_router.get("/connection", response_model=JiraConnectionStatusResponse)
def jira_connection_status(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> JiraConnectionStatusResponse:
    """Report Jira connection status for the current org (ops-safe; no tokens)."""
    with use_org(org_id):
        view = connection_status(get_active_connection(db))
    return JiraConnectionStatusResponse(
        status=view.status,
        connected=view.connected,
        cloud_id=view.cloud_id,
        site_url=view.site_url,
        site_name=view.site_name,
        scopes=view.scopes,
        access_token_expires_at=view.access_token_expires_at,
        detail=view.detail,
        updated_at=view.updated_at,
    )


@admin_router.delete("/connection", response_model=JiraConnectionStatusResponse)
def jira_disconnect(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> JiraConnectionStatusResponse:
    """Disconnect Jira for the current org (soft-delete + clear credentials)."""
    with use_org(org_id):
        disconnect_jira(db, get_active_connection(db))
        view = connection_status(get_active_connection(db))
    return JiraConnectionStatusResponse(
        status=view.status,
        connected=view.connected,
        detail=view.detail,
    )


def _post_oauth_redirect(payload: dict[str, object]) -> RedirectResponse | JSONResponse:
    """Prefer browser return to the web onboarding wizard when WEB_APP_URL is set."""
    base = settings.web_app_url.strip().rstrip("/")
    if not base:
        return JSONResponse(payload)
    query = urlencode({"jira": "connected"})
    return RedirectResponse(
        url=f"{base}/onboarding?{query}",
        status_code=status.HTTP_302_FOUND,
    )


@router.get("/connectors/jira/oauth/callback", response_model=None)
def jira_oauth_callback(
    db: Annotated[Session, Depends(get_db)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
):
    """Atlassian redirects here after consent; stores encrypted per-org credentials."""
    if error:
        # Safe for ops: Atlassian error codes only, never tokens.
        detail = error_description or error
        base = settings.web_app_url.strip().rstrip("/")
        if base:
            query = urlencode({"jira": "error", "detail": str(detail)[:200]})
            return RedirectResponse(
                url=f"{base}/onboarding?{query}",
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Atlassian authorization denied: {detail}",
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing code or state query parameter",
        )
    try:
        connection = complete_oauth_callback(db, code=code, state=state)
    except AtlassianOAuthError as exc:
        base = settings.web_app_url.strip().rstrip("/")
        if base:
            query = urlencode({"jira": "error", "detail": str(exc)[:200]})
            return RedirectResponse(
                url=f"{base}/onboarding?{query}",
                status_code=status.HTTP_302_FOUND,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    payload = {
        "status": connection.status.value,
        "connected": True,
        "cloud_id": connection.cloud_id,
        "site_url": connection.site_url,
        "site_name": connection.site_name,
    }
    return _post_oauth_redirect(payload)
