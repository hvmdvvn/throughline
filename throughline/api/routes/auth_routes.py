"""Authenticated identity / session helpers (issue #8)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.api.auth import AuthPrincipal, require_auth, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import MeResponse
from throughline.db.models import Membership
from throughline.tenancy import skip_tenant_enforcement

router = APIRouter(tags=["auth"])


@router.get("/me", response_model=MeResponse)
def read_me(
    principal: Annotated[AuthPrincipal, Depends(require_auth)],
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> MeResponse:
    """Return the local user and org resolved from membership (JWT required).

    First authenticated call creates/links ``User`` + bootstrap ``Membership``.
    Org context is derived from membership (see ``resolve_org_from_membership``).
    """
    membership = db.scalar(
        skip_tenant_enforcement(
            select(Membership).where(
                Membership.user_id == principal.user.id,
                Membership.org_id == org_id,
                Membership.deleted_at.is_(None),
            )
        )
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User has no org membership",
        )
    return MeResponse(
        user_id=principal.user.id,
        auth_subject=principal.user.auth_subject,
        email=principal.user.email,
        display_name=principal.user.display_name,
        org_id=org_id,
        membership_id=membership.id,
        role=membership.role,
    )
