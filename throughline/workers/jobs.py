"""arq job functions (issue #9).

Retries use exponential backoff via ``arq.Retry(defer=...)``. Callers that need
a retry should raise::

    raise Retry(defer=exponential_backoff_seconds(ctx["job_try"]))

``job_try`` is 1-based (first attempt is 1). Defer delays grow as
``base * 2 ** (job_try - 1)`` seconds (see ``EXAMPLE_JOB_BACKOFF_BASE_SECONDS``
and ``WorkerSettings.max_tries`` / per-job ``max_tries``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Retry / result visibility defaults (loaded by WorkerSettings and tests).
EXAMPLE_JOB_MAX_TRIES = 5
EXAMPLE_JOB_KEEP_RESULT_SECONDS = 3600
EXAMPLE_JOB_BACKOFF_BASE_SECONDS = 1.0


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
