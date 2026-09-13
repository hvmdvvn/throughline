"""arq worker settings (foundation example job + Phase 0 Jira import)."""

from __future__ import annotations

from typing import ClassVar

from arq.connections import RedisSettings
from arq.worker import func

from throughline.config import settings
from throughline.workers.jobs import (
    CHANGELOG_JOB_KEEP_RESULT_SECONDS,
    CHANGELOG_JOB_MAX_TRIES,
    EXAMPLE_JOB_KEEP_RESULT_SECONDS,
    EXAMPLE_JOB_MAX_TRIES,
    IMPORT_JOB_KEEP_RESULT_SECONDS,
    IMPORT_JOB_MAX_TRIES,
    ONBOARDING_JOB_KEEP_RESULT_SECONDS,
    ONBOARDING_JOB_MAX_TRIES,
    REPORT_JOB_KEEP_RESULT_SECONDS,
    REPORT_JOB_MAX_TRIES,
    example_sleep_log,
    generate_diagnostic_report_job,
    import_jira_changelog,
    import_jira_issue_history,
    run_diagnostic_onboarding,
)


async def ping(ctx: dict) -> str:
    """No-op job proving worker wiring (issue #2)."""
    return "pong"


# Example job with explicit keep_result / max_tries (issue #9). Exponential backoff
# for retries is documented in ``throughline.workers.jobs`` (raise ``Retry(defer=...)``).
example_sleep_log_job = func(
    example_sleep_log,
    name="example_sleep_log",
    keep_result=EXAMPLE_JOB_KEEP_RESULT_SECONDS,
    max_tries=EXAMPLE_JOB_MAX_TRIES,
)

# Resumable Jira issue history backfill (issue #13). Cursor lives in sync_state /
# connection progress; arq max_tries covers transient worker crashes.
import_jira_issue_history_job = func(
    import_jira_issue_history,
    name="import_jira_issue_history",
    keep_result=IMPORT_JOB_KEEP_RESULT_SECONDS,
    max_tries=IMPORT_JOB_MAX_TRIES,
)

# Resumable changelog status-transition import (issue #14). Sibling sync_state
# cursor is the last completed issue_key.
import_jira_changelog_job = func(
    import_jira_changelog,
    name="import_jira_changelog",
    keep_result=CHANGELOG_JOB_KEEP_RESULT_SECONDS,
    max_tries=CHANGELOG_JOB_MAX_TRIES,
)

# Versioned diagnostic report snapshot (issue #22). Each enqueue creates a new
# report version for the org + date range; prior versions stay immutable.
generate_diagnostic_report_arq_job = func(
    generate_diagnostic_report_job,
    name="generate_diagnostic_report",
    keep_result=REPORT_JOB_KEEP_RESULT_SECONDS,
    max_tries=REPORT_JOB_MAX_TRIES,
)

# Guided diagnostic onboarding orchestrator (issue #27): import → changelog →
# report → email. Progress is on diagnostic_onboardings for client polling.
run_diagnostic_onboarding_job = func(
    run_diagnostic_onboarding,
    name="run_diagnostic_onboarding",
    keep_result=ONBOARDING_JOB_KEEP_RESULT_SECONDS,
    max_tries=ONBOARDING_JOB_MAX_TRIES,
)


class WorkerSettings:
    """Run with: ``arq throughline.workers.settings.WorkerSettings``.

    Retry policy
    ------------
    - ``retry_jobs=True`` — honour ``arq.Retry`` and re-queue failed tries.
    - ``max_tries`` / per-job ``max_tries`` — cap attempts (default 5).
    - Exponential backoff — jobs raise ``Retry(defer=exponential_backoff_seconds(ctx["job_try"]))``
      (see ``throughline.workers.jobs``). arq has no separate Celery-style
      ``backoff`` knob; defer on ``Retry`` is the supported mechanism.

    Result visibility
    -----------------
    Completed job results are stored in Redis for ``keep_result`` seconds
    (arq result backend). Inspect via ``Job.status()`` / ``Job.info()`` or
    ``GET /admin/jobs/{job_id}``.
    """

    functions: ClassVar[list] = [
        ping,
        example_sleep_log_job,
        import_jira_issue_history_job,
        import_jira_changelog_job,
        generate_diagnostic_report_arq_job,
        run_diagnostic_onboarding_job,
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    # Worker-wide defaults (per-job func() overrides win when set).
    max_tries = EXAMPLE_JOB_MAX_TRIES
    keep_result = EXAMPLE_JOB_KEEP_RESULT_SECONDS
    retry_jobs = True
