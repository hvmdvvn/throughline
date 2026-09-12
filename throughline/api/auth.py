"""Temporary admin auth until hosted JWT verification (issue #8).

Accepts ``Authorization: Bearer <ADMIN_API_KEY>``. Swap this dependency for
Clerk/WorkOS JWT verification in issue #8 without changing route signatures.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Header, HTTPException, status

from throughline.config import settings


def require_admin(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Reject requests that lack a valid admin bearer token (401)."""
    expected = settings.admin_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API key is not configured",
        )
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
