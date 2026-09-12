"""Jira changelog import admin routes (issue #14)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import (
    JiraChangelogEnqueueResponse,
    JiraChangelogProgressResponse,
)
from throughline.connectors.jira.import_changelog import changelog_progress_view
from throughline.connectors.jira.service import get_active_connection
from throughline.tenancy import use_org
from throughline.workers.pool import arq_redis_pool

admin_router = APIRouter(
    prefix="/admin/jira",
    tags=["jira-changelog"],
    dependencies=[Depends(require_admin)],
)


@admin_router.get("/changelog", response_model=JiraChangelogProgressResponse)
def get_changelog_progress(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> JiraChangelogProgressResponse:
    """Return changelog import progress from the connection / sync_state."""
    with use_org(org_id):
        view = changelog_progress_view(db)
    return JiraChangelogProgressResponse(
        status=view.status,
        imported_count=view.imported_count,
        total_estimate=view.total_estimate,
        cursor=view.cursor,
        updated_at=view.updated_at,
        detail=view.detail,
        sync_status=view.sync_status,
    )


@admin_router.post("/changelog", response_model=JiraChangelogEnqueueResponse)
async def enqueue_changelog_import(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> JiraChangelogEnqueueResponse:
    """Enqueue the resumable arq job that imports status transitions."""
    with use_org(org_id):
        connection = get_active_connection(db)
        if connection is None or connection.cloud_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Jira is not connected for this organization",
            )

    async with arq_redis_pool() as redis:
        job = await redis.enqueue_job(
            "import_jira_changelog",
            str(org_id),
        )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not enqueue changelog import job",
        )
    return JiraChangelogEnqueueResponse(job_id=job.job_id, org_id=org_id)
