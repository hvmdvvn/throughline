"""Jira issue history import admin routes (issue #13)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import (
    JiraImportEnqueueResponse,
    JiraImportProgressResponse,
)
from throughline.connectors.jira.import_history import import_progress_view
from throughline.connectors.jira.service import get_active_connection
from throughline.tenancy import use_org
from throughline.workers.pool import arq_redis_pool

admin_router = APIRouter(
    prefix="/admin/jira",
    tags=["jira-import"],
    dependencies=[Depends(require_admin)],
)


class JiraImportRequest(BaseModel):
    """Optional JQL override for the history backfill job."""

    jql: str | None = Field(default=None, max_length=4000)


@admin_router.get("/import", response_model=JiraImportProgressResponse)
def get_import_progress(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> JiraImportProgressResponse:
    """Return issue-history import progress from the connection / sync_state."""
    with use_org(org_id):
        view = import_progress_view(db)
    return JiraImportProgressResponse(
        status=view.status,
        imported_count=view.imported_count,
        total_estimate=view.total_estimate,
        cursor=view.cursor,
        updated_at=view.updated_at,
        detail=view.detail,
        sync_status=view.sync_status,
    )


@admin_router.post("/import", response_model=JiraImportEnqueueResponse)
async def enqueue_issue_history_import(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
    body: JiraImportRequest | None = None,
) -> JiraImportEnqueueResponse:
    """Enqueue the resumable arq job that pages Jira issues via JQL."""
    with use_org(org_id):
        connection = get_active_connection(db)
        if connection is None or connection.cloud_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Jira is not connected for this organization",
            )

    jql = body.jql if body is not None else None
    async with arq_redis_pool() as redis:
        job = await redis.enqueue_job(
            "import_jira_issue_history",
            str(org_id),
            jql,
        )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not enqueue import job",
        )
    return JiraImportEnqueueResponse(job_id=job.job_id, org_id=org_id)
