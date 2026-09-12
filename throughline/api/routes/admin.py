"""Authenticated admin list endpoints for Org, User, and Membership (issue #6).

Not Django admin — FastAPI routes gated by Clerk JWT auth (``require_admin`` /
``require_auth`` from issue #8; replaces the temporary ``ADMIN_API_KEY`` stub).

``Org`` / ``User`` are not tenant-scoped (no ``org_id`` filter). ``Membership`` is
tenant-scoped; the platform-admin list uses an explicit
``skip_tenant_enforcement`` escape hatch so cross-org admin inventory still works
without a current-org context (see ``throughline.tenancy``).

Job status (issue #9) reads arq results from Redis — same ``REDIS_URL`` as the worker.
"""

from __future__ import annotations

from typing import Annotated

from arq.jobs import Job, JobStatus
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin
from throughline.api.deps import get_db
from throughline.api.schemas import (
    JobStatusResponse,
    MembershipListItem,
    OrgListItem,
    UserListItem,
)
from throughline.db.models import Membership, Org, User
from throughline.tenancy import skip_tenant_enforcement
from throughline.workers.pool import arq_redis_pool

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
    """List active memberships across orgs (platform admin; soft-deleted excluded)."""
    stmt = skip_tenant_enforcement(
        select(Membership)
        .where(Membership.deleted_at.is_(None))
        .order_by(Membership.created_at)
    )
    return list(db.scalars(stmt).all())


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Return arq job status / result for ``job_id`` (Redis result backend)."""
    if not job_id.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="job_id is required",
        )

    async with arq_redis_pool() as redis:
        job = Job(job_id, redis)
        job_status = await job.status()
        info = await job.info()

    response = JobStatusResponse(job_id=job_id, status=job_status.value)
    if info is not None:
        response.function = info.function
        response.job_try = info.job_try
        response.enqueue_time = info.enqueue_time
        response.score = info.score
        if hasattr(info, "success"):
            response.success = info.success
            response.result = info.result
            response.start_time = info.start_time
            response.finish_time = info.finish_time

    if job_status is JobStatus.not_found and info is None:
        # Still return a body so clients can poll; status is terminal ``not_found``.
        return response

    return response
