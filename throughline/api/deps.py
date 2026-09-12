"""FastAPI dependencies (DB session, current org, etc.)."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Annotated

from fastapi import Header, HTTPException, status
from sqlalchemy.orm import Session

from throughline.db.session import get_session_factory
from throughline.tenancy import get_current_org_id, require_current_org_id


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy session."""
    session_factory = get_session_factory()
    with session_factory() as session:
        yield session


def require_current_org(
    x_org_id: Annotated[uuid.UUID | None, Header(alias="X-Org-Id")] = None,
) -> uuid.UUID:
    """Require a current organization for the request.

    Until hosted auth (issue #8), clients pass ``X-Org-Id``. The HTTP middleware
    binds that header into the request-scoped contextvar (needed so sync route
    bodies / DB work see the same org). This dependency rejects requests with no
    org and returns the resolved id for injection into handlers.
    """
    if x_org_id is not None:
        return x_org_id
    existing = get_current_org_id()
    if existing is not None:
        return existing
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="X-Org-Id header is required",
    )


def get_required_org_id() -> uuid.UUID:
    """Return current org from contextvar or raise ``TenantContextError``."""
    return require_current_org_id()
