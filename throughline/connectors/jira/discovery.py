"""Jira project / field discovery and per-org field mapping (issue #12).

Fetches site metadata through ``JiraClient`` and upserts tenant-scoped rows.
Canonical concepts (acceptance criteria, story points, …) resolve only via
``jira_field_mappings`` — never via hardcoded Jira field ids or names in
application lookup paths.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.connectors.jira.client import JiraAPIError, JiraClient, build_client_from_tokens
from throughline.connectors.jira.oauth import AtlassianOAuthError
from throughline.connectors.jira.service import get_active_connection, get_valid_access_token
from throughline.db.models import (
    JiraFieldConcept,
    JiraFieldDefinition,
    JiraFieldMapping,
    JiraIssueType,
    JiraStatus,
    Project,
)
from throughline.tenancy import skip_tenant_enforcement

# Heuristic *seeding* only: candidate display-name matches used when a concept
# has no mapping yet. Runtime analytics must call ``resolve_mapped_field_id``.
_HEURISTIC_NAME_CANDIDATES: dict[JiraFieldConcept, frozenset[str]] = {
    JiraFieldConcept.ACCEPTANCE_CRITERIA: frozenset(
        {
            "acceptance criteria",
            "acceptance criterion",
            "ac",
        }
    ),
    JiraFieldConcept.STORY_POINTS: frozenset(
        {
            "story points",
            "story point",
            "story points estimate",
            "story point estimate",
        }
    ),
}


@dataclass(frozen=True)
class DiscoveryResult:
    """Counts from a discovery run (ops-safe)."""

    projects: int
    issue_types: int
    statuses: int
    fields: int
    mappings_seeded: int


def build_client_for_connection(db: Session, *, opener: Any | None = None) -> JiraClient:
    """Build a ``JiraClient`` from the current org's connected credentials."""
    connection = get_active_connection(db)
    if connection is None or connection.cloud_id is None:
        raise AtlassianOAuthError("Jira is not connected for this organization")
    token = get_valid_access_token(db, connection)
    kwargs: dict[str, Any] = {}
    if opener is not None:
        kwargs["opener"] = opener
    return build_client_from_tokens(
        access_token=token,
        cloud_id=connection.cloud_id,
        **kwargs,
    )


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_name(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _soft_delete_missing(
    rows: Sequence[Project | JiraIssueType | JiraStatus | JiraFieldDefinition],
    seen_keys: set[str],
    *,
    key_attr: str,
) -> None:
    now = datetime.now(UTC)
    for row in rows:
        key = getattr(row, key_attr)
        if key not in seen_keys and row.deleted_at is None:
            row.deleted_at = now


def _upsert_projects(db: Session, org_id: uuid.UUID, items: Iterable[Mapping[str, Any]]) -> int:
    existing = {
        row.external_id: row
        for row in db.scalars(
            skip_tenant_enforcement(
                select(Project).where(Project.org_id == org_id)
            )
        ).all()
    }
    seen: set[str] = set()
    count = 0
    for raw in items:
        external_id = _as_str(raw.get("id"))
        key = _as_str(raw.get("key"))
        name = _as_str(raw.get("name")) or key or external_id
        if not external_id or not key or not name:
            continue
        seen.add(external_id)
        row = existing.get(external_id)
        if row is None:
            row = Project(org_id=org_id, external_id=external_id, key=key, name=name)
            db.add(row)
            existing[external_id] = row
        else:
            row.key = key
            row.name = name
            row.deleted_at = None
        count += 1
    _soft_delete_missing(list(existing.values()), seen, key_attr="external_id")
    return count


def _upsert_issue_types(
    db: Session, org_id: uuid.UUID, items: Iterable[Mapping[str, Any]]
) -> int:
    existing = {
        row.external_id: row
        for row in db.scalars(
            skip_tenant_enforcement(
                select(JiraIssueType).where(JiraIssueType.org_id == org_id)
            )
        ).all()
    }
    seen: set[str] = set()
    count = 0
    for raw in items:
        external_id = _as_str(raw.get("id"))
        name = _as_str(raw.get("name"))
        if not external_id or not name:
            continue
        seen.add(external_id)
        description = _as_str(raw.get("description"))
        hierarchy = raw.get("hierarchyLevel")
        hierarchy_level = hierarchy if isinstance(hierarchy, int) else None
        row = existing.get(external_id)
        if row is None:
            row = JiraIssueType(
                org_id=org_id,
                external_id=external_id,
                name=name,
                description=description,
                hierarchy_level=hierarchy_level,
            )
            db.add(row)
            existing[external_id] = row
        else:
            row.name = name
            row.description = description
            row.hierarchy_level = hierarchy_level
            row.deleted_at = None
        count += 1
    _soft_delete_missing(list(existing.values()), seen, key_attr="external_id")
    return count


def _upsert_statuses(db: Session, org_id: uuid.UUID, items: Iterable[Mapping[str, Any]]) -> int:
    existing = {
        row.external_id: row
        for row in db.scalars(
            skip_tenant_enforcement(
                select(JiraStatus).where(JiraStatus.org_id == org_id)
            )
        ).all()
    }
    seen: set[str] = set()
    count = 0
    for raw in items:
        external_id = _as_str(raw.get("id"))
        name = _as_str(raw.get("name"))
        if not external_id or not name:
            continue
        seen.add(external_id)
        category = raw.get("statusCategory")
        category_key = None
        if isinstance(category, Mapping):
            category_key = _as_str(category.get("key"))
        row = existing.get(external_id)
        if row is None:
            row = JiraStatus(
                org_id=org_id,
                external_id=external_id,
                name=name,
                status_category_key=category_key,
            )
            db.add(row)
            existing[external_id] = row
        else:
            row.name = name
            row.status_category_key = category_key
            row.deleted_at = None
        count += 1
    _soft_delete_missing(list(existing.values()), seen, key_attr="external_id")
    return count


def _upsert_fields(db: Session, org_id: uuid.UUID, items: Iterable[Mapping[str, Any]]) -> int:
    existing = {
        row.field_id: row
        for row in db.scalars(
            skip_tenant_enforcement(
                select(JiraFieldDefinition).where(JiraFieldDefinition.org_id == org_id)
            )
        ).all()
    }
    seen: set[str] = set()
    count = 0
    for raw in items:
        field_id = _as_str(raw.get("id"))
        name = _as_str(raw.get("name"))
        if not field_id or not name:
            continue
        seen.add(field_id)
        custom = bool(raw.get("custom", False))
        schema = raw.get("schema")
        schema_type = None
        schema_custom = None
        if isinstance(schema, Mapping):
            schema_type = _as_str(schema.get("type"))
            schema_custom = _as_str(schema.get("custom"))
        row = existing.get(field_id)
        if row is None:
            row = JiraFieldDefinition(
                org_id=org_id,
                field_id=field_id,
                name=name,
                custom=custom,
                schema_type=schema_type,
                schema_custom=schema_custom,
            )
            db.add(row)
            existing[field_id] = row
        else:
            row.name = name
            row.custom = custom
            row.schema_type = schema_type
            row.schema_custom = schema_custom
            row.deleted_at = None
        count += 1
    _soft_delete_missing(list(existing.values()), seen, key_attr="field_id")
    return count


def _list_as_mappings(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("values", "statuses", "issueTypes"):
            values = payload.get(key)
            if isinstance(values, list):
                return [item for item in values if isinstance(item, Mapping)]
    raise JiraAPIError("Unexpected Jira discovery payload shape")


def seed_heuristic_mappings(
    db: Session,
    org_id: uuid.UUID,
    *,
    only_unmapped: bool = True,
) -> int:
    """Seed ``jira_field_mappings`` from discovered field names when unmapped.

    Does not overwrite existing mappings when ``only_unmapped`` is True.
    """
    fields = list(
        db.scalars(
            select(JiraFieldDefinition).where(JiraFieldDefinition.deleted_at.is_(None))
        ).all()
    )
    by_name: dict[str, list[JiraFieldDefinition]] = {}
    for field in fields:
        by_name.setdefault(_normalize_name(field.name), []).append(field)

    existing = {
        row.concept: row
        for row in db.scalars(
            skip_tenant_enforcement(
                select(JiraFieldMapping).where(JiraFieldMapping.org_id == org_id)
            )
        ).all()
    }

    seeded = 0
    for concept, candidates in _HEURISTIC_NAME_CANDIDATES.items():
        current = existing.get(concept)
        if only_unmapped and current is not None and current.deleted_at is None:
            continue
        match: JiraFieldDefinition | None = None
        for candidate in candidates:
            hits = by_name.get(candidate) or []
            # Prefer custom fields when multiple share a display name.
            custom_hits = [h for h in hits if h.custom]
            pool = custom_hits or hits
            if pool:
                match = pool[0]
                break
        if match is None:
            continue
        if current is None:
            row = JiraFieldMapping(
                org_id=org_id,
                concept=concept,
                jira_field_id=match.field_id,
            )
            db.add(row)
            existing[concept] = row
        else:
            current.jira_field_id = match.field_id
            current.deleted_at = None
        seeded += 1
    return seeded


def resolve_mapped_field_id(db: Session, concept: JiraFieldConcept) -> str | None:
    """Return the org's mapped Jira field id for ``concept``, or ``None``.

    Lookup is data-driven from ``jira_field_mappings`` only.
    """
    row = db.scalar(
        select(JiraFieldMapping).where(
            JiraFieldMapping.concept == concept,
            JiraFieldMapping.deleted_at.is_(None),
        )
    )
    if row is None:
        return None
    return row.jira_field_id


def set_field_mapping(
    db: Session,
    org_id: uuid.UUID,
    concept: JiraFieldConcept,
    jira_field_id: str,
) -> JiraFieldMapping:
    """Create or update a per-org concept → field mapping (manual ops path)."""
    field_id = jira_field_id.strip()
    if not field_id:
        raise ValueError("jira_field_id is required")

    existing = db.scalar(
        skip_tenant_enforcement(
            select(JiraFieldMapping).where(
                JiraFieldMapping.org_id == org_id,
                JiraFieldMapping.concept == concept,
            )
        )
    )
    if existing is None:
        row = JiraFieldMapping(org_id=org_id, concept=concept, jira_field_id=field_id)
        db.add(row)
        db.flush()
        return row
    existing.jira_field_id = field_id
    existing.deleted_at = None
    db.flush()
    return existing


def run_discovery(
    db: Session,
    client: JiraClient,
    org_id: uuid.UUID,
    *,
    seed_mappings: bool = True,
) -> DiscoveryResult:
    """Fetch projects, issue types, statuses, and fields; upsert for ``org_id``."""
    projects_raw = client.get_all("/project/search", list_key="values")
    issue_types_raw = _list_as_mappings(client.get("/issuetype"))
    statuses_raw = _list_as_mappings(client.get("/status"))
    fields_raw = _list_as_mappings(client.get("/field"))

    project_count = _upsert_projects(
        db, org_id, [p for p in projects_raw if isinstance(p, Mapping)]
    )
    issue_type_count = _upsert_issue_types(db, org_id, issue_types_raw)
    status_count = _upsert_statuses(db, org_id, statuses_raw)
    field_count = _upsert_fields(db, org_id, fields_raw)

    mappings_seeded = 0
    if seed_mappings:
        # Flush so heuristic seeding sees newly upserted field definitions.
        db.flush()
        mappings_seeded = seed_heuristic_mappings(db, org_id, only_unmapped=True)

    db.commit()
    return DiscoveryResult(
        projects=project_count,
        issue_types=issue_type_count,
        statuses=status_count,
        fields=field_count,
        mappings_seeded=mappings_seeded,
    )


def list_projects(db: Session) -> list[Project]:
    """Return active discovered projects for the current org."""
    return list(
        db.scalars(
            select(Project)
            .where(Project.deleted_at.is_(None))
            .order_by(Project.key)
        ).all()
    )


def list_field_mappings(db: Session) -> list[JiraFieldMapping]:
    """Return active field mappings for the current org."""
    return list(
        db.scalars(
            select(JiraFieldMapping)
            .where(JiraFieldMapping.deleted_at.is_(None))
            .order_by(JiraFieldMapping.concept)
        ).all()
    )
