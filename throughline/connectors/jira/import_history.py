"""Resumable Jira issue history import via JQL pagination (issue #13).

Pages ``/search`` with ``JiraClient``, upserts ``jira_issues``, and persists a
``sync_state`` cursor (next ``startAt``) after each successful page so killing
the worker mid-run continues rather than restarting from zero. Progress is
mirrored onto the org's ``JiraConnection`` for the admin API.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.connectors.jira.client import JiraAPIError, JiraClient
from throughline.connectors.jira.discovery import (
    build_client_for_connection,
    resolve_mapped_field_id,
)
from throughline.connectors.jira.service import get_active_connection
from throughline.db.models import (
    JiraConnection,
    JiraFieldConcept,
    JiraIssue,
    SyncRunStatus,
    SyncState,
)
from throughline.ingest.normalize import load_org_field_map, sync_canonical_issue_from_jira_payload
from throughline.tenancy import skip_tenant_enforcement

logger = logging.getLogger(__name__)

CONNECTOR_JIRA = "jira"
SYNC_KEY_ISSUE_HISTORY = "issue_history"
DEFAULT_IMPORT_JQL = "ORDER BY key ASC"
DEFAULT_PAGE_SIZE = 50
# Fields needed for Phase 0 analytics scaffolding; comments deferred to #69.
# Mapped custom-field ids are appended at runtime from per-org mappings (#12/#15).
_SEARCH_FIELDS_BASE = (
    "summary,status,issuetype,project,created,updated,description,"
    "labels,priority,assignee,reporter"
)


def _search_fields(db: Session) -> str:
    """Compose /search fields list including org-mapped custom fields when set."""
    parts = [_SEARCH_FIELDS_BASE]
    for concept in JiraFieldConcept:
        field_id = resolve_mapped_field_id(db, concept)
        if field_id:
            parts.append(field_id)
    return ",".join(parts)


@dataclass(frozen=True)
class ImportProgressView:
    """Ops-safe import progress for the authenticated admin API."""

    status: str
    imported_count: int
    total_estimate: int | None
    cursor: str | None
    updated_at: datetime | None
    detail: str | None = None
    sync_status: str | None = None


@dataclass(frozen=True)
class ImportResult:
    """Outcome of one import job attempt."""

    imported_count: int
    total_estimate: int | None
    cursor: str | None
    completed: bool
    pages_processed: int


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


def _nested_id_name(obj: Any) -> tuple[str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    id_val = obj.get("id")
    name_val = obj.get("name")
    return (
        str(id_val) if id_val is not None else None,
        str(name_val) if isinstance(name_val, str) else None,
    )


def _project_key(fields: dict[str, Any]) -> str | None:
    project = fields.get("project")
    if isinstance(project, dict):
        key = project.get("key")
        if isinstance(key, str) and key.strip():
            return key.strip()
    return None


def get_or_create_issue_history_sync_state(db: Session, org_id: uuid.UUID) -> SyncState:
    """Return the org's issue-history sync_state row (create if missing)."""
    row = db.scalar(
        select(SyncState).where(
            SyncState.connector == CONNECTOR_JIRA,
            SyncState.sync_key == SYNC_KEY_ISSUE_HISTORY,
        )
    )
    if row is not None:
        return row
    # Soft-deleted row reuse (same unique key).
    row = db.scalar(
        skip_tenant_enforcement(
            select(SyncState).where(
                SyncState.org_id == org_id,
                SyncState.connector == CONNECTOR_JIRA,
                SyncState.sync_key == SYNC_KEY_ISSUE_HISTORY,
            )
        )
    )
    if row is not None:
        row.deleted_at = None
        return row
    row = SyncState(
        org_id=org_id,
        connector=CONNECTOR_JIRA,
        sync_key=SYNC_KEY_ISSUE_HISTORY,
        status=SyncRunStatus.IDLE,
        imported_count=0,
    )
    db.add(row)
    db.flush()
    return row


def _cursor_to_start_at(cursor: str | None) -> int:
    if cursor is None or not str(cursor).strip():
        return 0
    try:
        return max(0, int(str(cursor).strip()))
    except ValueError:
        return 0


def _mirror_progress_to_connection(
    connection: JiraConnection | None,
    sync: SyncState,
) -> None:
    if connection is None:
        return
    connection.import_status = sync.status.value
    connection.import_imported_count = sync.imported_count
    connection.import_total_estimate = sync.total_estimate
    connection.import_cursor = sync.cursor
    connection.import_updated_at = datetime.now(UTC)


def import_progress_view(db: Session) -> ImportProgressView:
    """Build progress from the connection record (fallback: sync_state)."""
    connection = get_active_connection(db)
    sync = db.scalar(
        select(SyncState).where(
            SyncState.connector == CONNECTOR_JIRA,
            SyncState.sync_key == SYNC_KEY_ISSUE_HISTORY,
        )
    )
    if connection is not None and connection.import_status is not None:
        return ImportProgressView(
            status=connection.import_status,
            imported_count=connection.import_imported_count,
            total_estimate=connection.import_total_estimate,
            cursor=connection.import_cursor,
            updated_at=connection.import_updated_at,
            detail=sync.detail if sync is not None else None,
            sync_status=sync.status.value if sync is not None else None,
        )
    if sync is None:
        return ImportProgressView(
            status=SyncRunStatus.IDLE.value,
            imported_count=0,
            total_estimate=None,
            cursor=None,
            updated_at=None,
        )
    return ImportProgressView(
        status=sync.status.value,
        imported_count=sync.imported_count,
        total_estimate=sync.total_estimate,
        cursor=sync.cursor,
        updated_at=sync.last_success_at or sync.updated_at,
        detail=sync.detail,
        sync_status=sync.status.value,
    )


def _upsert_issue(
    db: Session,
    org_id: uuid.UUID,
    issue: dict[str, Any],
    *,
    field_map: dict[JiraFieldConcept, str | None] | None = None,
) -> None:
    key = issue.get("key")
    external_id = issue.get("id")
    if not isinstance(key, str) or not key.strip():
        raise JiraAPIError("Search issue missing key")
    if external_id is None:
        raise JiraAPIError(f"Search issue {key} missing id")
    key = key.strip()
    ext = str(external_id).strip()
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    assert isinstance(fields, dict)

    status_id, status_name = _nested_id_name(fields.get("status"))
    type_id, type_name = _nested_id_name(fields.get("issuetype"))
    summary = fields.get("summary")
    summary_str = summary if isinstance(summary, str) else None

    existing = db.scalar(select(JiraIssue).where(JiraIssue.issue_key == key))
    if existing is None:
        # May exist under soft-delete or be brand new.
        existing = db.scalar(
            skip_tenant_enforcement(
                select(JiraIssue).where(
                    JiraIssue.org_id == org_id,
                    JiraIssue.issue_key == key,
                )
            )
        )

    payload = json.dumps(issue, separators=(",", ":"), default=str)
    if existing is None:
        row = JiraIssue(
            org_id=org_id,
            external_id=ext,
            issue_key=key,
            project_key=_project_key(fields),
            summary=summary_str,
            status_name=status_name,
            status_id=status_id,
            issue_type_name=type_name,
            issue_type_id=type_id,
            raw_json=payload,
            jira_created_at=_parse_jira_datetime(fields.get("created")),
            jira_updated_at=_parse_jira_datetime(fields.get("updated")),
        )
        db.add(row)
    else:
        existing.deleted_at = None
        existing.external_id = ext
        existing.project_key = _project_key(fields)
        existing.summary = summary_str
        existing.status_name = status_name
        existing.status_id = status_id
        existing.issue_type_name = type_name
        existing.issue_type_id = type_id
        existing.raw_json = payload
        existing.jira_created_at = _parse_jira_datetime(fields.get("created"))
        existing.jira_updated_at = _parse_jira_datetime(fields.get("updated"))

    # Canonical row for analytics — Jira field ids stay out of domain models (#15).
    sync_canonical_issue_from_jira_payload(db, org_id, issue, field_map=field_map)


def run_issue_history_import(
    db: Session,
    client: JiraClient,
    org_id: uuid.UUID,
    *,
    jql: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
) -> ImportResult:
    """Page JQL search results into ``jira_issues``, committing after each page.

    Resumes from ``sync_state.cursor`` (next ``startAt``). Mid-run failures leave
    the cursor advanced past the last committed page.
    """
    if page_size < 1:
        raise ValueError("page_size must be >= 1")
    query = (jql or DEFAULT_IMPORT_JQL).strip() or DEFAULT_IMPORT_JQL
    connection = get_active_connection(db)
    sync = get_or_create_issue_history_sync_state(db, org_id)

    # Fresh completed run: restart from zero (idempotent upserts).
    start_at = 0
    if sync.status == SyncRunStatus.COMPLETED:
        sync.cursor = None
        sync.imported_count = 0
        sync.total_estimate = None
        sync.detail = None
    else:
        start_at = _cursor_to_start_at(sync.cursor)

    sync.status = SyncRunStatus.RUNNING
    sync.detail = None
    _mirror_progress_to_connection(connection, sync)
    db.commit()

    search_fields = _search_fields(db)
    field_map = load_org_field_map(db)
    pages_processed = 0
    try:
        while True:
            if max_pages is not None and pages_processed >= max_pages:
                # Soft stop for tests: leave RUNNING + cursor for resume.
                db.commit()
                return ImportResult(
                    imported_count=sync.imported_count,
                    total_estimate=sync.total_estimate,
                    cursor=sync.cursor,
                    completed=False,
                    pages_processed=pages_processed,
                )

            payload = client.get(
                "/search",
                params={
                    "jql": query,
                    "startAt": start_at,
                    "maxResults": page_size,
                    "fields": search_fields,
                },
            )
            if not isinstance(payload, dict):
                raise JiraAPIError("Expected a JSON object from /search")

            issues = payload.get("issues")
            if not isinstance(issues, list):
                raise JiraAPIError("Search response missing issues list")

            total = payload.get("total")
            if isinstance(total, int):
                sync.total_estimate = total

            for issue in issues:
                if not isinstance(issue, dict):
                    raise JiraAPIError("Search issue entry is not an object")
                _upsert_issue(db, org_id, issue, field_map=field_map)

            pages_processed += 1
            page_len = len(issues)
            # Cursor advances only after a successful page persist.
            next_start = start_at + page_len
            if isinstance(total, int) and next_start >= total:
                sync.cursor = str(next_start)
                sync.imported_count = total
                sync.status = SyncRunStatus.COMPLETED
                sync.last_success_at = datetime.now(UTC)
                sync.detail = None
                _mirror_progress_to_connection(connection, sync)
                db.commit()
                return ImportResult(
                    imported_count=sync.imported_count,
                    total_estimate=sync.total_estimate,
                    cursor=sync.cursor,
                    completed=True,
                    pages_processed=pages_processed,
                )

            if page_len == 0 or page_len < page_size:
                # Short / empty page without reliable total — treat as done.
                sync.cursor = str(next_start)
                sync.imported_count = next_start
                sync.status = SyncRunStatus.COMPLETED
                sync.last_success_at = datetime.now(UTC)
                sync.detail = None
                _mirror_progress_to_connection(connection, sync)
                db.commit()
                return ImportResult(
                    imported_count=sync.imported_count,
                    total_estimate=sync.total_estimate,
                    cursor=sync.cursor,
                    completed=True,
                    pages_processed=pages_processed,
                )

            sync.cursor = str(next_start)
            sync.imported_count = next_start
            sync.last_success_at = datetime.now(UTC)
            _mirror_progress_to_connection(connection, sync)
            db.commit()
            start_at = next_start

    except Exception as exc:
        # Drop uncommitted page upserts; keep the last committed cursor.
        db.rollback()
        sync = get_or_create_issue_history_sync_state(db, org_id)
        connection = get_active_connection(db)
        detail = str(exc)[:500] if str(exc) else "Import failed"
        sync.status = SyncRunStatus.FAILED
        sync.detail = detail
        _mirror_progress_to_connection(connection, sync)
        db.commit()
        logger.exception(
            "jira issue history import failed org_id=%s cursor=%s",
            org_id,
            sync.cursor,
        )
        raise


def run_issue_history_import_for_org(
    db: Session,
    org_id: uuid.UUID,
    *,
    jql: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    max_pages: int | None = None,
    opener: Any | None = None,
) -> ImportResult:
    """Build a ``JiraClient`` from the org connection and run the import."""
    client = build_client_for_connection(db, opener=opener)
    return run_issue_history_import(
        db,
        client,
        org_id,
        jql=jql,
        page_size=page_size,
        max_pages=max_pages,
    )
