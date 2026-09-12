"""Authenticated admin list endpoints for Org, User, and Membership (issue #6).

Not Django admin — FastAPI routes gated by ``require_admin`` (temporary API-key
stub until issue #8).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin
from throughline.api.deps import get_db
from throughline.api.schemas import MembershipListItem, OrgListItem, UserListItem
from throughline.db.models import Membership, Org, User

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


@router.get("/orgs", response_model=list[OrgListItem])
def list_orgs(db: Annotated[Session, Depends(get_db)]) -> list[Org]:
    """List active organizations (soft-deleted rows excluded)."""
    return list(
        db.scalars(select(Org).where(Org.deleted_at.is_(None)).order_by(Org.created_at)).all()
    )


@router.get("/users", response_model=list[UserListItem])
def list_users(db: Annotated[Session, Depends(get_db)]) -> list[User]:
    """List active users (soft-deleted rows excluded)."""
    return list(
        db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.created_at)).all()
    )


@router.get("/memberships", response_model=list[MembershipListItem])
def list_memberships(db: Annotated[Session, Depends(get_db)]) -> list[Membership]:
    """List active memberships (soft-deleted rows excluded)."""
    return list(
        db.scalars(
            select(Membership)
            .where(Membership.deleted_at.is_(None))
            .order_by(Membership.created_at)
        ).all()
    )
