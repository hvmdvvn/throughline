"""Resumable Jira changelog import for status transitions (issue #14).

For each imported ``jira_issues`` row, fetches ``/issue/{key}/changelog`` via
``JiraClient``, stores status transitions with timestamps and actors, and
persists a sibling ``sync_state`` cursor (last completed ``issue_key``) so a
killed worker resumes without reprocessing the whole set. Progress is mirrored
onto the org's ``JiraConnection`` for the admin API.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from throughline.connectors.jira.client import JiraAPIError, JiraClient
from throughline.connectors.jira.discovery import build_client_for_connection
from throughline.connectors.jira.service import get_active_connection
from throughline.db.models import (
    JiraConnection,
    JiraIssue,
    JiraStatusTransition,
    SyncRunStatus,
    SyncState,
)
from throughline.ingest.normalize import sync_canonical_transitions_for_issue
from throughline.tenancy import skip_tenant_enforcement

logger = logging.getLogger(__name__)

CONNECTOR_JIRA = "jira"
SYNC_KEY_CHANGELOG = "changelog"
DEFAULT_CHANGELOG_PAGE_SIZE = 100
STATUS_FIELD = "status"


@dataclass(frozen=True)
class ChangelogProgressView:
    """Ops-safe changelog import progress for the authenticated admin API."""

    status: str
    imported_count: int
    total_estimate: int | None
    cursor: str | None
    updated_at: datetime | None
    detail: str | None = None
    sync_status: str | None = None


@dataclass(frozen=True)
class ChangelogImportResult:
    """Outcome of one changelog import job attempt."""

    imported_count: int
    total_estimate: int | None
    cursor: str | None
    completed: bool
    issues_processed: int
    transitions_stored: int


def _parse_jira_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    # Jira often returns "+0000" without a colon.
    if len(raw) >= 5 and (raw[-5] in "+-") and raw[-3] != ":":
        raw = raw[:-2] + ":" + raw[-2:]
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def get_or_create_changelog_sync_state(db: Session, org_id: uuid.UUID) -> SyncState:
    """Return the org's changelog sync_state row (create if missing)."""
    row = db.scalar(
        select(SyncState).where(
            SyncState.connector == CONNECTOR_JIRA,
            SyncState.sync_key == SYNC_KEY_CHANGELOG,
        )
    )
    if row is not None:
        return row
    row = db.scalar(
        skip_tenant_enforcement(
            select(SyncState).where(
                SyncState.org_id == org_id,
                SyncState.connector == CONNECTOR_JIRA,
                SyncState.sync_key == SYNC_KEY_CHANGELOG,
            )
        )
    )
    if row is not None:
        row.deleted_at = None
        return row
    row = SyncState(
        org_id=org_id,
        connector=CONNECTOR_JIRA,
        sync_key=SYNC_KEY_CHANGELOG,
        status=SyncRunStatus.IDLE,
        imported_count=0,
    )
    db.add(row)
    db.flush()
    return row


def _mirror_progress_to_connection(
    connection: JiraConnection | None,
    sync: SyncState,
) -> None:
    if connection is None:
        return
    connection.changelog_status = sync.status.value
    connection.changelog_imported_count = sync.imported_count
    connection.changelog_total_estimate = sync.total_estimate
    connection.changelog_cursor = sync.cursor
    connection.changelog_updated_at = datetime.now(UTC)


def changelog_progress_view(db: Session) -> ChangelogProgressView:
    """Build progress from the connection record (fallback: sync_state)."""
    connection = get_active_connection(db)
    sync = db.scalar(
        select(SyncState).where(
            SyncState.connector == CONNECTOR_JIRA,
            SyncState.sync_key == SYNC_KEY_CHANGELOG,
        )
    )
    if connection is not None and connection.changelog_status is not None:
        return ChangelogProgressView(
            status=connection.changelog_status,
            imported_count=connection.changelog_imported_count,
            total_estimate=connection.changelog_total_estimate,
            cursor=connection.changelog_cursor,
            updated_at=connection.changelog_updated_at,
            detail=sync.detail if sync is not None else None,
            sync_status=sync.status.value if sync is not None else None,
        )
    if sync is None:
        return ChangelogProgressView(
            status=SyncRunStatus.IDLE.value,
            imported_count=0,
            total_estimate=None,
            cursor=None,
            updated_at=None,
        )
    return ChangelogProgressView(
        status=sync.status.value,
        imported_count=sync.imported_count,
        total_estimate=sync.total_estimate,
        cursor=sync.cursor,
        updated_at=sync.last_success_at or sync.updated_at,
        detail=sync.detail,
        sync_status=sync.status.value,
    )


def _actor_fields(author: Any) -> tuple[str | None, str | None]:
    if not isinstance(author, dict):
        return None, None
    account_id = author.get("accountId")
    display = author.get("displayName")
    return (
        str(account_id) if account_id is not None else None,
        str(display) if isinstance(display, str) else None,
    )


def _status_item_fields(
    item: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    from_id = item.get("from")
    to_id = item.get("to")
    from_name = item.get("fromString")
    to_name = item.get("toString")
    return (
        str(from_id) if from_id is not None else None,
        str(from_name) if isinstance(from_name, str) else None,
        str(to_id) if to_id is not None else None,
        str(to_name) if isinstance(to_name, str) else None,
    )


def _existing_transition(
    db: Session,
    org_id: uuid.UUID,
    *,
    issue_key: str,
    history_id: str,
    item_index: int,
) -> JiraStatusTransition | None:
    row = db.scalar(
        select(JiraStatusTransition).where(
            JiraStatusTransition.issue_key == issue_key,
            JiraStatusTransition.history_id == history_id,
            JiraStatusTransition.item_index == item_index,
        )
    )
    if row is not None:
        return row
    return db.scalar(
        skip_tenant_enforcement(
            select(JiraStatusTransition).where(
                JiraStatusTransition.org_id == org_id,
                JiraStatusTransition.issue_key == issue_key,
                JiraStatusTransition.history_id == history_id,
                JiraStatusTransition.item_index == item_index,
            )
        )
    )


def _upsert_status_transition(
    db: Session,
    org_id: uuid.UUID,
    *,
    issue_key: str,
    history_id: str,
    item_index: int,
    transitioned_at: datetime,
    actor_account_id: str | None,
    actor_display_name: str | None,
    from_status_id: str | None,
    from_status_name: str | None,
    to_status_id: str | None,
    to_status_name: str | None,
) -> bool:
    """Insert or revive a transition row. Returns True when a new row was added."""
    existing = _existing_transition(
        db,
        org_id,
        issue_key=issue_key,
        history_id=history_id,
        item_index=item_index,
    )
    if existing is None:
        db.add(
            JiraStatusTransition(
                org_id=org_id,
                issue_key=issue_key,
                history_id=history_id,
                item_index=item_index,
                transitioned_at=transitioned_at,
                actor_account_id=actor_account_id,
                actor_display_name=actor_display_name,
                from_status_id=from_status_id,
                from_status_name=from_status_name,
                to_status_id=to_status_id,
                to_status_name=to_status_name,
            )
        )
        return True

    existing.deleted_at = None
    existing.transitioned_at = transitioned_at
    existing.actor_account_id = actor_account_id
    existing.actor_display_name = actor_display_name
    existing.from_status_id = from_status_id
    existing.from_status_name = from_status_name
    existing.to_status_id = to_status_id
    existing.to_status_name = to_status_name
    return False


def _store_status_transitions_from_history(
    db: Session,
    org_id: uuid.UUID,
    *,
    issue_key: str,
    history: dict[str, Any],
) -> int:
    """Extract status items from one changelog history entry. Returns new row count."""
    history_id = history.get("id")
    if history_id is None:
        raise JiraAPIError(f"Changelog history for {issue_key} missing id")
    hid = str(history_id).strip()
    if not hid:
        raise JiraAPIError(f"Changelog history for {issue_key} has empty id")

    transitioned_at = _parse_jira_datetime(history.get("created"))
    if transitioned_at is None:
        raise JiraAPIError(f"Changelog history {hid} for {issue_key} missing created")

    actor_account_id, actor_display_name = _actor_fields(history.get("author"))
    items = history.get("items")
    if not isinstance(items, list):
        return 0

    stored = 0
    status_index = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if not isinstance(field, str) or field.lower() != STATUS_FIELD:
            continue
        from_id, from_name, to_id, to_name = _status_item_fields(item)
        if _upsert_status_transition(
            db,
            org_id,
            issue_key=issue_key,
            history_id=hid,
            item_index=status_index,
            transitioned_at=transitioned_at,
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            from_status_id=from_id,
            from_status_name=from_name,
            to_status_id=to_id,
            to_status_name=to_name,
        ):
            stored += 1
        status_index += 1
    return stored


def _import_issue_changelog(
    db: Session,
    client: JiraClient,
    org_id: uuid.UUID,
    issue: JiraIssue,
    *,
    page_size: int,
) -> int:
    """Fetch all changelog pages for one issue and store status transitions."""
    path = f"/issue/{issue.issue_key}/changelog"
    transitions_stored = 0
    for page in client.iter_pages(path, page_size=page_size, list_key="values"):
        values = page.get("values")
        if not isinstance(values, list):
            raise JiraAPIError(
                f"Changelog response for {issue.issue_key} missing values list"
            )
        for history in values:
            if not isinstance(history, dict):
                raise JiraAPIError(
                    f"Changelog history entry for {issue.issue_key} is not an object"
                )
            transitions_stored += _store_status_transitions_from_history(
                db,
                org_id,
                issue_key=issue.issue_key,
                history=history,
            )
    # Canonical transitions for analytics (issue #15) — no Jira field ids.
    sync_canonical_transitions_for_issue(db, org_id, issue.issue_key)
    issue.changelog_imported_at = datetime.now(UTC)
    return transitions_stored


def _pending_issues(
    db: Session,
    *,
    after_key: str | None,
) -> list[JiraIssue]:
    """Issues still needing a changelog pass, ordered by issue_key."""
    stmt = (
        select(JiraIssue)
        .where(JiraIssue.changelog_imported_at.is_(None))
        .order_by(JiraIssue.issue_key.asc())
    )
    if after_key is not None and str(after_key).strip():
        stmt = stmt.where(JiraIssue.issue_key > str(after_key).strip())
    return list(db.scalars(stmt).all())


def _count_issues(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(JiraIssue)) or 0)


def _count_changelog_complete(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(JiraIssue)
            .where(JiraIssue.changelog_imported_at.is_not(None))
        )
        or 0
    )


def run_changelog_import(
    db: Session,
    client: JiraClient,
    org_id: uuid.UUID,
    *,
    page_size: int = DEFAULT_CHANGELOG_PAGE_SIZE,
    max_issues: int | None = None,
) -> ChangelogImportResult:
    """Import status transitions for pending issues; commit after each issue.

    Resumes from ``sync_state.cursor`` (last completed issue_key). Issues with
    ``changelog_imported_at`` set are skipped on re-run (no duplicate rows).
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")

    connection = get_active_connection(db)
    sync = get_or_create_changelog_sync_state(db, org_id)
    total_estimate = _count_issues(db)

    # Fresh completed run: clear cursor so newly imported issues (no flag) are
    # picked up; already-flagged issues stay skipped.
    after_key: str | None = None
    if sync.status == SyncRunStatus.COMPLETED:
        sync.cursor = None
        sync.detail = None
    else:
        after_key = sync.cursor

    sync.status = SyncRunStatus.RUNNING
    sync.total_estimate = total_estimate
    sync.imported_count = _count_changelog_complete(db)
    sync.detail = None
    _mirror_progress_to_connection(connection, sync)
    db.commit()

    issues_processed = 0
    transitions_stored = 0
    try:
        pending = _pending_issues(db, after_key=after_key)
        for issue in pending:
            if max_issues is not None and issues_processed >= max_issues:
                # Soft stop for tests: leave RUNNING + cursor for resume.
                db.commit()
                return ChangelogImportResult(
                    imported_count=sync.imported_count,
                    total_estimate=sync.total_estimate,
                    cursor=sync.cursor,
                    completed=False,
                    issues_processed=issues_processed,
                    transitions_stored=transitions_stored,
                )

            added = _import_issue_changelog(
                db, client, org_id, issue, page_size=page_size
            )
            transitions_stored += added
            issues_processed += 1
            db.flush()
            sync.cursor = issue.issue_key
            sync.imported_count = _count_changelog_complete(db)
            sync.total_estimate = _count_issues(db)
            sync.last_success_at = datetime.now(UTC)
            _mirror_progress_to_connection(connection, sync)
            db.commit()

        db.flush()
        sync.status = SyncRunStatus.COMPLETED
        sync.imported_count = _count_changelog_complete(db)
        sync.total_estimate = _count_issues(db)
        sync.last_success_at = datetime.now(UTC)
        sync.detail = None
        _mirror_progress_to_connection(connection, sync)
        db.commit()
        return ChangelogImportResult(
            imported_count=sync.imported_count,
            total_estimate=sync.total_estimate,
            cursor=sync.cursor,
            completed=True,
            issues_processed=issues_processed,
            transitions_stored=transitions_stored,
        )
    except Exception as exc:
        db.rollback()
        sync = get_or_create_changelog_sync_state(db, org_id)
        connection = get_active_connection(db)
        detail = str(exc)[:500] if str(exc) else "Changelog import failed"
        sync.status = SyncRunStatus.FAILED
        sync.detail = detail
        _mirror_progress_to_connection(connection, sync)
        db.commit()
        logger.exception(
            "jira changelog import failed org_id=%s cursor=%s",
            org_id,
            sync.cursor,
        )
        raise


def run_changelog_import_for_org(
    db: Session,
    org_id: uuid.UUID,
    *,
    page_size: int = DEFAULT_CHANGELOG_PAGE_SIZE,
    max_issues: int | None = None,
    opener: Any | None = None,
) -> ChangelogImportResult:
    """Build a ``JiraClient`` from the org connection and run the changelog import."""
    client = build_client_for_connection(db, opener=opener)
    return run_changelog_import(
        db,
        client,
        org_id,
        page_size=page_size,
        max_issues=max_issues,
    )
