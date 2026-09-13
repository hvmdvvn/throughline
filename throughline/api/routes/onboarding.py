"""Diagnostic onboarding API (issue #27).

Guided flow: OAuth → issue import → changelog → report → email.
Progress is polled via GET; long work runs in arq.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from throughline.api.auth import require_admin, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import DiagnosticOnboardingProgressResponse
from throughline.db.models import DiagnosticOnboarding
from throughline.onboarding.diagnostic import (
    DiagnosticOnboardingError,
    OnboardingProgressView,
    advance_after_oauth,
    get_onboarding,
    progress_view,
    reset_for_retry,
    start_or_resume,
)
from throughline.tenancy import use_org
from throughline.workers.pool import arq_redis_pool

router = APIRouter(
    prefix="/onboarding/diagnostic",
    tags=["onboarding"],
    dependencies=[Depends(require_admin)],
)


class DiagnosticOnboardingStartRequest(BaseModel):
    """Start or restart the guided diagnostic onboarding flow."""

    notify_email: str = Field(min_length=3, max_length=320)
    range_start: date
    range_end: date
    jql: str | None = Field(default=None, max_length=4000)


def _to_response(view: OnboardingProgressView) -> DiagnosticOnboardingProgressResponse:
    return DiagnosticOnboardingProgressResponse(
        id=view.id,
        org_id=view.org_id,
        stage=view.stage,
        notify_email=view.notify_email,
        range_start=view.range_start,
        range_end=view.range_end,
        jql=view.jql,
        progress_status=view.progress_status,
        progress_imported_count=view.progress_imported_count,
        progress_total_estimate=view.progress_total_estimate,
        progress_detail=view.progress_detail,
        progress_updated_at=view.progress_updated_at,
        orchestrator_job_id=view.orchestrator_job_id,
        report_id=view.report_id,
        email_status=view.email_status,
        email_detail=view.email_detail,
        email_sent_at=view.email_sent_at,
        failed_stage=view.failed_stage,
        error_message=view.error_message,
        completed_at=view.completed_at,
        authorize_url=view.authorize_url,
        report_url=view.report_url,
        created_at=view.created_at,
        updated_at=view.updated_at,
        recoverable=view.recoverable,
    )


async def _enqueue_and_record(db: Session, session: DiagnosticOnboarding) -> None:
    async with arq_redis_pool() as redis:
        job = await redis.enqueue_job(
            "run_diagnostic_onboarding",
            str(session.id),
        )
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not enqueue onboarding job",
        )
    session.orchestrator_job_id = job.job_id
    db.commit()
    db.refresh(session)


@router.get("", response_model=DiagnosticOnboardingProgressResponse)
def get_diagnostic_onboarding(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> DiagnosticOnboardingProgressResponse:
    """Return the current org onboarding session (or 404 if never started)."""
    with use_org(org_id):
        session = get_onboarding(db)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No diagnostic onboarding session for this organization",
            )
        return _to_response(progress_view(db, session, include_authorize_url=True))


@router.post("", response_model=DiagnosticOnboardingProgressResponse)
async def start_diagnostic_onboarding(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
    body: DiagnosticOnboardingStartRequest,
) -> DiagnosticOnboardingProgressResponse:
    """Start (or resume in-flight) the guided diagnostic onboarding flow."""
    with use_org(org_id):
        try:
            session, needs_job = start_or_resume(
                db,
                org_id,
                notify_email=body.notify_email,
                range_start=body.range_start,
                range_end=body.range_end,
                jql=body.jql,
            )
        except DiagnosticOnboardingError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        db.commit()
        db.refresh(session)

        if needs_job:
            await _enqueue_and_record(db, session)

        return _to_response(progress_view(db, session, include_authorize_url=True))


@router.post("/continue", response_model=DiagnosticOnboardingProgressResponse)
async def continue_diagnostic_onboarding(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> DiagnosticOnboardingProgressResponse:
    """After OAuth, advance into import + report generation."""
    with use_org(org_id):
        try:
            session, needs_job = advance_after_oauth(db, org_id)
        except DiagnosticOnboardingError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        db.commit()
        db.refresh(session)

        if needs_job:
            await _enqueue_and_record(db, session)

        return _to_response(progress_view(db, session, include_authorize_url=True))


@router.post("/retry", response_model=DiagnosticOnboardingProgressResponse)
async def retry_diagnostic_onboarding(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> DiagnosticOnboardingProgressResponse:
    """Recover from a failed stage without leaving the org stuck running."""
    with use_org(org_id):
        try:
            session, needs_job = reset_for_retry(db)
        except DiagnosticOnboardingError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        db.commit()
        db.refresh(session)

        if needs_job:
            await _enqueue_and_record(db, session)

        return _to_response(progress_view(db, session, include_authorize_url=True))
