"""arq job functions (issue #9 / #13 / #14 / #22).

Retries use exponential backoff via ``arq.Retry(defer=...)``. Callers that need
a retry should raise::

    raise Retry(defer=exponential_backoff_seconds(ctx["job_try"]))

``job_try`` is 1-based (first attempt is 1). Defer delays grow as
``base * 2 ** (job_try - 1)`` seconds (see ``EXAMPLE_JOB_BACKOFF_BASE_SECONDS``
and ``WorkerSettings.max_tries`` / per-job ``max_tries``).

Issue history import (issue #13) and changelog import (issue #14) persist their
own ``sync_state`` cursors so a killed worker continues mid-backfill; arq
retries are secondary to those cursors.

Diagnostic report generation (issue #22) snapshots analytics into a versioned
``diagnostic_reports`` row; failures leave ``failed`` / ``partial`` status.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from typing import Any

from throughline.analytics.diagnostic_report import generate_diagnostic_report
from throughline.connectors.jira.import_changelog import run_changelog_import_for_org
from throughline.connectors.jira.import_history import run_issue_history_import_for_org
from throughline.db.session import get_session_factory
from throughline.tenancy import use_org

logger = logging.getLogger(__name__)

# Retry / result visibility defaults (loaded by WorkerSettings and tests).
EXAMPLE_JOB_MAX_TRIES = 5
EXAMPLE_JOB_KEEP_RESULT_SECONDS = 3600
EXAMPLE_JOB_BACKOFF_BASE_SECONDS = 1.0

IMPORT_JOB_MAX_TRIES = 5
IMPORT_JOB_KEEP_RESULT_SECONDS = 3600

CHANGELOG_JOB_MAX_TRIES = 5
CHANGELOG_JOB_KEEP_RESULT_SECONDS = 3600

REPORT_JOB_MAX_TRIES = 5
REPORT_JOB_KEEP_RESULT_SECONDS = 3600


def exponential_backoff_seconds(
    job_try: int,
    *,
    base_seconds: float = EXAMPLE_JOB_BACKOFF_BASE_SECONDS,
) -> float:
    """Return defer delay for attempt ``job_try`` (1-based) with exponential growth."""
    if job_try < 1:
        raise ValueError("job_try must be >= 1")
    return float(base_seconds) * (2 ** (job_try - 1))


async def example_sleep_log(ctx: dict[str, Any]) -> dict[str, Any]:
    """Example job: brief sleep + log line; returns a small JSON-friendly payload."""
    job_try = int(ctx.get("job_try") or 1)
    logger.info("example_sleep_log start job_try=%s", job_try)
    await asyncio.sleep(0.05)
    logger.info("example_sleep_log done job_try=%s", job_try)
    return {"ok": True, "job_try": job_try}


async def import_jira_issue_history(
    ctx: dict[str, Any],
    org_id: str,
    jql: str | None = None,
) -> dict[str, Any]:
    """Backfill Jira issues for ``org_id`` via resumable JQL pagination (issue #13)."""
    _ = ctx
    org_uuid = uuid.UUID(org_id)
    session_factory = get_session_factory()

    def _run() -> dict[str, Any]:
        with session_factory() as db:
            with use_org(org_uuid):
                result = run_issue_history_import_for_org(db, org_uuid, jql=jql)
            return {
                "ok": True,
                "org_id": str(org_uuid),
                "imported_count": result.imported_count,
                "total_estimate": result.total_estimate,
                "cursor": result.cursor,
                "completed": result.completed,
                "pages_processed": result.pages_processed,
            }

    return await asyncio.to_thread(_run)


async def import_jira_changelog(
    ctx: dict[str, Any],
    org_id: str,
) -> dict[str, Any]:
    """Backfill status transitions for imported issues (issue #14)."""
    _ = ctx
    org_uuid = uuid.UUID(org_id)
    session_factory = get_session_factory()

    def _run() -> dict[str, Any]:
        with session_factory() as db:
            with use_org(org_uuid):
                result = run_changelog_import_for_org(db, org_uuid)
            return {
                "ok": True,
                "org_id": str(org_uuid),
                "imported_count": result.imported_count,
                "total_estimate": result.total_estimate,
                "cursor": result.cursor,
                "completed": result.completed,
                "issues_processed": result.issues_processed,
                "transitions_stored": result.transitions_stored,
            }

    return await asyncio.to_thread(_run)


async def generate_diagnostic_report_job(
    ctx: dict[str, Any],
    org_id: str,
    range_start: str,
    range_end: str,
) -> dict[str, Any]:
    """Generate a versioned diagnostic report for ``org_id`` + date range (#22).

    ``range_start`` / ``range_end`` are ISO dates (``YYYY-MM-DD``). Each run
    inserts a new report version; prior versions are left untouched.
    """
    _ = ctx
    org_uuid = uuid.UUID(org_id)
    start = date.fromisoformat(range_start)
    end = date.fromisoformat(range_end)
    session_factory = get_session_factory()

    def _run() -> dict[str, Any]:
        with session_factory() as db:
            with use_org(org_uuid):
                report = generate_diagnostic_report(db, org_uuid, start, end)
            db.commit()
            return {
                "ok": report.status in {"success", "partial"},
                "org_id": str(org_uuid),
                "report_id": str(report.id),
                "version": report.version,
                "status": report.status,
                "range_start": report.range_start.isoformat(),
                "range_end": report.range_end.isoformat(),
                "metric_keys": sorted(report.metrics.keys()),
                "error_message": report.error_message,
            }

    return await asyncio.to_thread(_run)
