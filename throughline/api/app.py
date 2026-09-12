"""Minimal FastAPI application."""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request, Response

from throughline.api.routes import admin
from throughline.config import settings
from throughline.tenancy import reset_current_org_id, set_current_org_id

app = FastAPI(title=settings.app_name)
app.include_router(admin.router)


@app.middleware("http")
async def tenant_context_middleware(request: Request, call_next) -> Response:
    """Bind ``X-Org-Id`` into a request-scoped contextvar (cleared after the request)."""
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
