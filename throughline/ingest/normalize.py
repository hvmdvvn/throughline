"""Persist canonical issues/transitions from connector-stored payloads (issue #15).

Uses per-org field mappings (#12). Jira payload parsing stays in
``throughline.connectors.jira.normalize``; this module upserts domain-shaped
ORM rows that analytics can read without Jira client types.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.connectors.jira.normalize import (
    map_jira_issue_payload,
    map_jira_issue_raw_json,
    map_jira_status_transition,
)
from throughline.db.models import (
    Issue,
    IssueTransition,
    JiraFieldConcept,
    JiraFieldMapping,
    JiraIssue,
    JiraStatusTransition,
)
from throughline.domain.issues import CanonicalIssue, CanonicalTransition
from throughline.tenancy import skip_tenant_enforcement


def load_org_field_map(db: Session) -> dict[JiraFieldConcept, str | None]:
    """Return concept → Jira field id for the current tenant (missing → ``None``)."""
    mapping: dict[JiraFieldConcept, str | None] = {
        JiraFieldConcept.ACCEPTANCE_CRITERIA: None,
        JiraFieldConcept.STORY_POINTS: None,
    }
    rows = db.scalars(
        select(JiraFieldMapping).where(JiraFieldMapping.deleted_at.is_(None))
    ).all()
    for row in rows:
        mapping[row.concept] = row.jira_field_id
    return mapping


def upsert_canonical_issue(db: Session, org_id: uuid.UUID, issue: CanonicalIssue) -> Issue:
    """Insert or update the tenant's ``issues`` row from a domain object."""
    existing = db.scalar(select(Issue).where(Issue.external_key == issue.external_key))
    if existing is None:
        existing = db.scalar(
            skip_tenant_enforcement(
                select(Issue).where(
                    Issue.org_id == org_id,
                    Issue.external_key == issue.external_key,
                )
            )
        )

    if existing is None:
        row = Issue(
            org_id=org_id,
            external_key=issue.external_key,
            project_key=issue.project_key,
            summary=issue.summary,
            status=issue.status,
            issue_type=issue.issue_type,
            acceptance_criteria=issue.acceptance_criteria,
            story_points=issue.story_points,
            source_created_at=issue.created_at,
            source_updated_at=issue.updated_at,
        )
        db.add(row)
        return row

    existing.deleted_at = None
    existing.project_key = issue.project_key
    existing.summary = issue.summary
    existing.status = issue.status
    existing.issue_type = issue.issue_type
    existing.acceptance_criteria = issue.acceptance_criteria
    existing.story_points = issue.story_points
    existing.source_created_at = issue.created_at
    existing.source_updated_at = issue.updated_at
    return existing


def upsert_canonical_transition(
    db: Session,
    org_id: uuid.UUID,
    transition: CanonicalTransition,
) -> bool:
    """Insert or update one transition. Returns True when a new row was created."""
    existing = db.scalar(
        select(IssueTransition).where(
            IssueTransition.external_key == transition.external_key,
            IssueTransition.external_event_id == transition.external_event_id,
            IssueTransition.event_index == transition.event_index,
        )
    )
    if existing is None:
        existing = db.scalar(
            skip_tenant_enforcement(
                select(IssueTransition).where(
                    IssueTransition.org_id == org_id,
                    IssueTransition.external_key == transition.external_key,
                    IssueTransition.external_event_id == transition.external_event_id,
                    IssueTransition.event_index == transition.event_index,
                )
            )
        )

    if existing is None:
        db.add(
            IssueTransition(
                org_id=org_id,
                external_key=transition.external_key,
                transitioned_at=transition.transitioned_at,
                from_status=transition.from_status,
                to_status=transition.to_status,
                actor_id=transition.actor_id,
                actor_display_name=transition.actor_display_name,
                external_event_id=transition.external_event_id,
                event_index=transition.event_index,
            )
        )
        return True

    existing.deleted_at = None
    existing.transitioned_at = transition.transitioned_at
    existing.from_status = transition.from_status
    existing.to_status = transition.to_status
    existing.actor_id = transition.actor_id
    existing.actor_display_name = transition.actor_display_name
    return False


def sync_canonical_issue_from_jira_payload(
    db: Session,
    org_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    field_map: dict[JiraFieldConcept, str | None] | None = None,
) -> Issue:
    """Map one Jira search/get issue dict and upsert the canonical ``issues`` row."""
    concepts = field_map if field_map is not None else load_org_field_map(db)
    canonical = map_jira_issue_payload(payload, concepts)
    return upsert_canonical_issue(db, org_id, canonical)


def sync_canonical_issue_from_jira_row(
    db: Session,
    org_id: uuid.UUID,
    jira_issue: JiraIssue,
    *,
    field_map: dict[JiraFieldConcept, str | None] | None = None,
) -> Issue:
    """Normalize one stored ``JiraIssue`` (via ``raw_json``) into ``issues``."""
    concepts = field_map if field_map is not None else load_org_field_map(db)
    canonical = map_jira_issue_raw_json(jira_issue.raw_json, concepts)
    if canonical is None:
        # Fall back to columns already denormalized on the Jira row.
        # Mapped custom fields require raw_json; absent → explicit None.
        canonical = CanonicalIssue(
            external_key=jira_issue.issue_key,
            project_key=jira_issue.project_key,
            summary=jira_issue.summary,
            status=jira_issue.status_name,
            issue_type=jira_issue.issue_type_name,
            acceptance_criteria=None,
            story_points=None,
            created_at=jira_issue.jira_created_at,
            updated_at=jira_issue.jira_updated_at,
        )
    return upsert_canonical_issue(db, org_id, canonical)


def sync_canonical_transitions_for_issue(
    db: Session,
    org_id: uuid.UUID,
    issue_key: str,
) -> int:
    """Upsert canonical transitions from stored ``jira_status_transitions`` for one key."""
    rows = list(
        db.scalars(
            select(JiraStatusTransition).where(
                JiraStatusTransition.issue_key == issue_key,
                JiraStatusTransition.deleted_at.is_(None),
            )
        ).all()
    )
    created = 0
    for row in rows:
        transition = map_jira_status_transition(
            issue_key=row.issue_key,
            history_id=row.history_id,
            item_index=row.item_index,
            transitioned_at=row.transitioned_at,
            from_status_name=row.from_status_name,
            to_status_name=row.to_status_name,
            actor_account_id=row.actor_account_id,
            actor_display_name=row.actor_display_name,
        )
        if upsert_canonical_transition(db, org_id, transition):
            created += 1
    return created


def normalize_issues_for_org(db: Session, org_id: uuid.UUID) -> int:
    """Re-normalize all active ``jira_issues`` for the org into ``issues``.

    Safe to re-run after field-mapping changes. Returns upsert count.
    """
    field_map = load_org_field_map(db)
    issues = list(
        db.scalars(select(JiraIssue).where(JiraIssue.deleted_at.is_(None))).all()
    )
    count = 0
    for jira_issue in issues:
        if sync_canonical_issue_from_jira_row(db, org_id, jira_issue, field_map=field_map):
            count += 1
    return count


def normalize_transitions_for_org(db: Session, org_id: uuid.UUID) -> int:
    """Re-normalize all stored Jira status transitions into ``issue_transitions``."""
    keys = list(
        db.scalars(
            select(JiraStatusTransition.issue_key)
            .where(JiraStatusTransition.deleted_at.is_(None))
            .distinct()
        ).all()
    )
    total = 0
    for key in keys:
        total += sync_canonical_transitions_for_issue(db, org_id, str(key))
    return total
