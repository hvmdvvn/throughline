"""arq job functions (issue #9 / #13).

Retries use exponential backoff via ``arq.Retry(defer=...)``. Callers that need
a retry should raise::

    raise Retry(defer=exponential_backoff_seconds(ctx["job_try"]))

``job_try`` is 1-based (first attempt is 1). Defer delays grow as
``base * 2 ** (job_try - 1)`` seconds (see ``EXAMPLE_JOB_BACKOFF_BASE_SECONDS``
and ``WorkerSettings.max_tries`` / per-job ``max_tries``).

Issue history import (issue #13) persists its own ``sync_state`` cursor so a
killed worker continues mid-backfill; arq retries are secondary to that cursor.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

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
