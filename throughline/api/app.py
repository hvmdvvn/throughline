"""Minimal FastAPI application."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from throughline.api.routes import (
    admin,
    auth_routes,
    jira_changelog,
    jira_discovery,
    jira_import,
    jira_oauth,
    reports,
)
from throughline.config import settings
from throughline.tenancy import reset_current_org_id, set_current_org_id

app = FastAPI(title=settings.app_name)

# Allow the Next.js app (``throughline/web``) to call authenticated API routes from the browser.
if settings.cors_origin_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(admin.router)
app.include_router(auth_routes.router)
app.include_router(reports.router)
app.include_router(jira_oauth.admin_router)
app.include_router(jira_oauth.router)
app.include_router(jira_discovery.admin_router)
app.include_router(jira_import.admin_router)
app.include_router(jira_changelog.admin_router)


@app.middleware("http")
async def tenant_context_middleware(request: Request, call_next) -> Response:
    """Bind ``X-Org-Id`` into a request-scoped contextvar (cleared after the request).

    Authenticated routes may also set org context from membership via
    ``resolve_org_from_membership`` when the header is omitted.
    """
    raw = request.headers.get("x-org-id")
    org_id: uuid.UUID | None = None
    if raw:
        try:
            org_id = uuid.UUID(raw)
        except ValueError:
            org_id = None
    token = set_current_org_id(org_id)
    try:
        return await call_next(request)
    finally:
        reset_current_org_id(token)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
