"""Diagnostic onboarding state machine (issue #27).

Sequences: OAuth → issue import → changelog → report generation → email.
Long-running work runs inside an arq job (``run_diagnostic_onboarding``);
progress is persisted on ``DiagnosticOnboarding`` so clients can leave and
poll. Failures set ``stage=failed`` with ``failed_stage`` / ``error_message``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.analytics.diagnostic_report import generate_diagnostic_report
from throughline.config import settings
from throughline.connectors.jira.import_changelog import (
    changelog_progress_view,
    run_changelog_import_for_org,
)
from throughline.connectors.jira.import_history import (
    import_progress_view,
    run_issue_history_import_for_org,
)
from throughline.connectors.jira.oauth import AtlassianOAuthError
from throughline.connectors.jira.service import (
    authorization_redirect_url,
    connection_status,
    get_active_connection,
)
from throughline.db.models import (
    DiagnosticOnboarding,
    DiagnosticOnboardingStage,
    SyncRunStatus,
)
from throughline.email.smtp import send_email
from throughline.tenancy import skip_tenant_enforcement, use_org

logger = logging.getLogger(__name__)

TERMINAL_STAGES = frozenset(
    {
        DiagnosticOnboardingStage.COMPLETED.value,
        DiagnosticOnboardingStage.FAILED.value,
    }
)

# Stages that mean arq work is in flight (or about to be).
RUNNING_STAGES = frozenset(
    {
        DiagnosticOnboardingStage.IMPORTING_ISSUES.value,
        DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value,
        DiagnosticOnboardingStage.GENERATING_REPORT.value,
        DiagnosticOnboardingStage.SENDING_EMAIL.value,
    }
)


class DiagnosticOnboardingError(RuntimeError):
    """Domain error for onboarding start / advance."""


@dataclass(frozen=True)
class OnboardingProgressView:
    """API-facing snapshot of the current onboarding session."""

    id: uuid.UUID
    org_id: uuid.UUID
    stage: str
    notify_email: str
    range_start: date
    range_end: date
    jql: str | None
    progress_status: str | None
    progress_imported_count: int
    progress_total_estimate: int | None
    progress_detail: str | None
    progress_updated_at: datetime | None
    orchestrator_job_id: str | None
    report_id: uuid.UUID | None
    email_status: str | None
    email_detail: str | None
    email_sent_at: datetime | None
    failed_stage: str | None
    error_message: str | None
    completed_at: datetime | None
    authorize_url: str | None
    report_url: str | None
    created_at: datetime
    updated_at: datetime
    recoverable: bool


def get_onboarding(db: Session) -> DiagnosticOnboarding | None:
    """Return the org's active (non-soft-deleted) onboarding row."""
    return db.scalar(select(DiagnosticOnboarding))


def _report_url(report_id: uuid.UUID | None) -> str | None:
    if report_id is None:
        return None
    base = settings.web_app_url.strip().rstrip("/")
    if not base:
        return f"/diagnostic?report={report_id}"
    return f"{base}/diagnostic?report={report_id}"


def progress_view(
    db: Session,
    session: DiagnosticOnboarding,
    *,
    include_authorize_url: bool = False,
) -> OnboardingProgressView:
    """Build a progress view, enriching live import/changelog counters when running."""
    progress_status = session.progress_status
    progress_count = session.progress_imported_count
    progress_total = session.progress_total_estimate
    progress_detail = session.progress_detail
    progress_updated = session.progress_updated_at

    if session.stage == DiagnosticOnboardingStage.IMPORTING_ISSUES.value:
        live = import_progress_view(db)
        progress_status = live.status or progress_status
        progress_count = live.imported_count
        progress_total = live.total_estimate
        progress_detail = live.detail or progress_detail
        progress_updated = live.updated_at or progress_updated
    elif session.stage == DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value:
        live = changelog_progress_view(db)
        progress_status = live.status or progress_status
        progress_count = live.imported_count
        progress_total = live.total_estimate
        progress_detail = live.detail or progress_detail
        progress_updated = live.updated_at or progress_updated

    authorize_url: str | None = None
    if include_authorize_url or session.stage == DiagnosticOnboardingStage.AWAITING_OAUTH.value:
        try:
            authorize_url = authorization_redirect_url(session.org_id)
        except AtlassianOAuthError:
            authorize_url = None

    return OnboardingProgressView(
        id=session.id,
        org_id=session.org_id,
        stage=session.stage,
        notify_email=session.notify_email,
        range_start=session.range_start,
        range_end=session.range_end,
        jql=session.jql,
        progress_status=progress_status,
        progress_imported_count=progress_count,
        progress_total_estimate=progress_total,
        progress_detail=progress_detail,
        progress_updated_at=progress_updated,
        orchestrator_job_id=session.orchestrator_job_id,
        report_id=session.report_id,
        email_status=session.email_status,
        email_detail=session.email_detail,
        email_sent_at=session.email_sent_at,
        failed_stage=session.failed_stage,
        error_message=session.error_message,
        completed_at=session.completed_at,
        authorize_url=authorize_url,
        report_url=_report_url(session.report_id),
        created_at=session.created_at,
        updated_at=session.updated_at,
        recoverable=session.stage == DiagnosticOnboardingStage.FAILED.value,
    )


def _reset_runtime_fields(session: DiagnosticOnboarding) -> None:
    session.progress_status = None
    session.progress_imported_count = 0
    session.progress_total_estimate = None
    session.progress_detail = None
    session.progress_updated_at = None
    session.orchestrator_job_id = None
    session.report_id = None
    session.email_status = None
    session.email_detail = None
    session.email_sent_at = None
    session.failed_stage = None
    session.error_message = None
    session.completed_at = None


def _jira_connected(db: Session) -> bool:
    view = connection_status(get_active_connection(db))
    return view.connected


def start_or_resume(
    db: Session,
    org_id: uuid.UUID,
    *,
    notify_email: str,
    range_start: date,
    range_end: date,
    jql: str | None = None,
) -> tuple[DiagnosticOnboarding, bool]:
    """Create or update the org onboarding session.

    Returns ``(session, needs_orchestrator)``. When Jira is not connected the
    session stays in ``awaiting_oauth`` and ``needs_orchestrator`` is False.
    """
    if range_end < range_start:
        raise DiagnosticOnboardingError("range_end must be on or after range_start")
    email = notify_email.strip()
    if not email or "@" not in email:
        raise DiagnosticOnboardingError("notify_email must be a valid email address")

    session = get_onboarding(db)
    if session is None:
        session = DiagnosticOnboarding(
            org_id=org_id,
            stage=DiagnosticOnboardingStage.AWAITING_OAUTH.value,
            notify_email=email,
            range_start=range_start,
            range_end=range_end,
            jql=jql,
        )
        db.add(session)
        db.flush()
    elif session.stage in RUNNING_STAGES:
        # Idempotent: return the in-flight session (user left and returned).
        return session, False
    else:
        session.notify_email = email
        session.range_start = range_start
        session.range_end = range_end
        session.jql = jql
        _reset_runtime_fields(session)

    if not _jira_connected(db):
        session.stage = DiagnosticOnboardingStage.AWAITING_OAUTH.value
        session.progress_detail = "Connect Jira to continue"
        session.progress_updated_at = datetime.now(UTC)
        db.flush()
        return session, False

    session.stage = DiagnosticOnboardingStage.IMPORTING_ISSUES.value
    session.progress_status = SyncRunStatus.IDLE.value
    session.progress_detail = "Queued issue history import"
    session.progress_updated_at = datetime.now(UTC)
    db.flush()
    return session, True


def advance_after_oauth(db: Session, org_id: uuid.UUID) -> tuple[DiagnosticOnboarding, bool]:
    """Move ``awaiting_oauth`` → import once Jira is connected."""
    _ = org_id
    session = get_onboarding(db)
    if session is None:
        raise DiagnosticOnboardingError("No onboarding session; start the flow first")
    if session.stage in RUNNING_STAGES:
        return session, False
    if session.stage == DiagnosticOnboardingStage.COMPLETED.value:
        return session, False
    if session.stage == DiagnosticOnboardingStage.FAILED.value:
        raise DiagnosticOnboardingError("Onboarding failed; use retry")

    if not _jira_connected(db):
        session.stage = DiagnosticOnboardingStage.AWAITING_OAUTH.value
        session.progress_detail = "Jira is not connected yet"
        session.progress_updated_at = datetime.now(UTC)
        db.flush()
        return session, False

    session.stage = DiagnosticOnboardingStage.IMPORTING_ISSUES.value
    session.failed_stage = None
    session.error_message = None
    session.progress_status = SyncRunStatus.IDLE.value
    session.progress_detail = "Queued issue history import"
    session.progress_updated_at = datetime.now(UTC)
    db.flush()
    return session, True


def reset_for_retry(db: Session) -> tuple[DiagnosticOnboarding, bool]:
    """Recover from ``failed`` by re-entering the pipeline from the failed stage."""
    session = get_onboarding(db)
    if session is None:
        raise DiagnosticOnboardingError("No onboarding session to retry")
    if session.stage != DiagnosticOnboardingStage.FAILED.value:
        raise DiagnosticOnboardingError("Onboarding is not in a failed state")

    resume_from = session.failed_stage or DiagnosticOnboardingStage.IMPORTING_ISSUES.value
    if resume_from == DiagnosticOnboardingStage.AWAITING_OAUTH.value:
        if not _jira_connected(db):
            session.stage = DiagnosticOnboardingStage.AWAITING_OAUTH.value
            session.failed_stage = None
            session.error_message = None
            session.progress_detail = "Connect Jira to continue"
            session.progress_updated_at = datetime.now(UTC)
            db.flush()
            return session, False
        resume_from = DiagnosticOnboardingStage.IMPORTING_ISSUES.value

    if resume_from not in RUNNING_STAGES:
        resume_from = DiagnosticOnboardingStage.IMPORTING_ISSUES.value

    session.stage = resume_from
    session.failed_stage = None
    session.error_message = None
    session.progress_status = SyncRunStatus.IDLE.value
    session.progress_detail = f"Retrying from {resume_from}"
    session.progress_updated_at = datetime.now(UTC)
    session.orchestrator_job_id = None
    db.flush()
    return session, True


def _mark_failed(session: DiagnosticOnboarding, stage: str, message: str) -> None:
    session.failed_stage = stage
    session.stage = DiagnosticOnboardingStage.FAILED.value
    session.error_message = message[:2000]
    session.progress_status = SyncRunStatus.FAILED.value
    session.progress_detail = message[:500]
    session.progress_updated_at = datetime.now(UTC)


def _set_stage_progress(
    session: DiagnosticOnboarding,
    *,
    stage: str,
    status: str,
    detail: str,
    imported_count: int = 0,
    total_estimate: int | None = None,
) -> None:
    session.stage = stage
    session.progress_status = status
    session.progress_detail = detail[:500]
    session.progress_imported_count = imported_count
    session.progress_total_estimate = total_estimate
    session.progress_updated_at = datetime.now(UTC)


def run_pipeline(db: Session, session_id: uuid.UUID) -> dict[str, Any]:
    """Execute remaining onboarding stages for ``session_id`` (arq worker).

    Idempotent for completed sessions. On any exception the session is marked
    ``failed`` so the org is never left in an unexplained running state.

    When an import returns ``completed=False`` (page budget / soft stop), the
    result includes ``needs_continue=True`` so the arq job can re-enqueue.
    """
    session = db.scalar(
        skip_tenant_enforcement(
            select(DiagnosticOnboarding).where(DiagnosticOnboarding.id == session_id)
        )
    )
    if session is None:
        raise DiagnosticOnboardingError(f"Onboarding session {session_id} not found")

    with use_org(session.org_id):
        return _run_pipeline_in_org(db, session)


def _run_pipeline_in_org(db: Session, session: DiagnosticOnboarding) -> dict[str, Any]:
    """Continue the pipeline with tenant context already bound."""
    session_id = session.id

    if session.stage == DiagnosticOnboardingStage.COMPLETED.value:
        return {
            "ok": True,
            "stage": session.stage,
            "report_id": str(session.report_id) if session.report_id else None,
            "already_completed": True,
        }

    if session.stage == DiagnosticOnboardingStage.FAILED.value:
        return {
            "ok": False,
            "stage": session.stage,
            "failed_stage": session.failed_stage,
            "error_message": session.error_message,
        }

    if session.stage == DiagnosticOnboardingStage.AWAITING_OAUTH.value:
        if not _jira_connected(db):
            return {"ok": False, "stage": session.stage, "waiting": "oauth"}
        session.stage = DiagnosticOnboardingStage.IMPORTING_ISSUES.value
        db.commit()

    active_stage = session.stage
    try:
        if session.stage == DiagnosticOnboardingStage.IMPORTING_ISSUES.value:
            active_stage = DiagnosticOnboardingStage.IMPORTING_ISSUES.value
            _set_stage_progress(
                session,
                stage=active_stage,
                status=SyncRunStatus.RUNNING.value,
                detail="Importing Jira issue history",
            )
            db.commit()
            result = run_issue_history_import_for_org(db, session.org_id, jql=session.jql)
            if not result.completed:
                _set_stage_progress(
                    session,
                    stage=active_stage,
                    status=SyncRunStatus.RUNNING.value,
                    detail="Issue import in progress (resumable)",
                    imported_count=result.imported_count,
                    total_estimate=result.total_estimate,
                )
                db.commit()
                return {
                    "ok": True,
                    "stage": session.stage,
                    "completed": False,
                    "needs_continue": True,
                    "imported_count": result.imported_count,
                }
            _set_stage_progress(
                session,
                stage=DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value,
                status=SyncRunStatus.IDLE.value,
                detail="Issue import complete; starting changelog",
                imported_count=result.imported_count,
                total_estimate=result.total_estimate,
            )
            db.commit()

        if session.stage == DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value:
            active_stage = DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value
            _set_stage_progress(
                session,
                stage=active_stage,
                status=SyncRunStatus.RUNNING.value,
                detail="Importing status transitions",
            )
            db.commit()
            result = run_changelog_import_for_org(db, session.org_id)
            if not result.completed:
                _set_stage_progress(
                    session,
                    stage=active_stage,
                    status=SyncRunStatus.RUNNING.value,
                    detail="Changelog import in progress (resumable)",
                    imported_count=result.imported_count,
                    total_estimate=result.total_estimate,
                )
                db.commit()
                return {
                    "ok": True,
                    "stage": session.stage,
                    "completed": False,
                    "needs_continue": True,
                    "imported_count": result.imported_count,
                }
            _set_stage_progress(
                session,
                stage=DiagnosticOnboardingStage.GENERATING_REPORT.value,
                status=SyncRunStatus.IDLE.value,
                detail="Changelog complete; generating report",
                imported_count=result.imported_count,
                total_estimate=result.total_estimate,
            )
            db.commit()

        if session.stage == DiagnosticOnboardingStage.GENERATING_REPORT.value:
            active_stage = DiagnosticOnboardingStage.GENERATING_REPORT.value
            _set_stage_progress(
                session,
                stage=active_stage,
                status=SyncRunStatus.RUNNING.value,
                detail="Running analytics and snapshotting report",
            )
            db.commit()
            report = generate_diagnostic_report(
                db,
                session.org_id,
                session.range_start,
                session.range_end,
            )
            session.report_id = report.id
            if report.status == "failed":
                raise DiagnosticOnboardingError(
                    report.error_message or "Report generation failed"
                )
            _set_stage_progress(
                session,
                stage=DiagnosticOnboardingStage.SENDING_EMAIL.value,
                status=SyncRunStatus.IDLE.value,
                detail="Report ready; sending notification email",
            )
            db.commit()

        if session.stage == DiagnosticOnboardingStage.SENDING_EMAIL.value:
            active_stage = DiagnosticOnboardingStage.SENDING_EMAIL.value
            _set_stage_progress(
                session,
                stage=active_stage,
                status=SyncRunStatus.RUNNING.value,
                detail="Sending report-ready email",
            )
            db.commit()
            report_link = _report_url(session.report_id) or "(open Throughline → Diagnostic)"
            delivery = send_email(
                to=session.notify_email,
                subject="Your Throughline diagnostic report is ready",
                body_text=(
                    "Your Throughline diagnostic report is ready.\n\n"
                    f"Open it here: {report_link}\n\n"
                    f"Date range: {session.range_start.isoformat()} → "
                    f"{session.range_end.isoformat()}\n"
                ),
            )
            session.email_status = delivery.status
            session.email_detail = delivery.detail
            if delivery.status == "sent":
                session.email_sent_at = datetime.now(UTC)
            if delivery.status == "failed":
                raise DiagnosticOnboardingError(
                    delivery.detail or "Failed to send report-ready email"
                )
            session.stage = DiagnosticOnboardingStage.COMPLETED.value
            session.progress_status = SyncRunStatus.COMPLETED.value
            session.progress_detail = "Diagnostic onboarding complete"
            session.progress_updated_at = datetime.now(UTC)
            session.completed_at = datetime.now(UTC)
            session.failed_stage = None
            session.error_message = None
            db.commit()

        return {
            "ok": True,
            "stage": session.stage,
            "report_id": str(session.report_id) if session.report_id else None,
            "email_status": session.email_status,
            "completed": session.stage == DiagnosticOnboardingStage.COMPLETED.value,
        }
    except Exception as exc:
        message = str(exc)[:2000] if str(exc) else "Onboarding pipeline failed"
        logger.exception(
            "diagnostic onboarding failed session_id=%s stage=%s",
            session_id,
            active_stage,
        )
        # Re-load after possible rollback inside import helpers.
        db.rollback()
        session = db.scalar(
            select(DiagnosticOnboarding).where(DiagnosticOnboarding.id == session_id)
        )
        if session is not None:
            _mark_failed(session, active_stage, message)
            db.commit()
        return {
            "ok": False,
            "stage": DiagnosticOnboardingStage.FAILED.value,
            "failed_stage": active_stage,
            "error_message": message,
        }

