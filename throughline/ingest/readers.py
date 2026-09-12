"""Read canonical issues/transitions for analytics (no connector imports).

Downstream Phase 0 metrics (#17-#21) should use this module (or the ORM
``Issue`` / ``IssueTransition`` models) rather than Jira client types.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.db.models import Issue, IssueTransition
from throughline.domain.issues import CanonicalIssue, CanonicalTransition


def list_canonical_issues(db: Session) -> list[CanonicalIssue]:
    """Return active canonical issues for the current tenant session."""
    rows = list(
        db.scalars(
            select(Issue).where(Issue.deleted_at.is_(None)).order_by(Issue.external_key.asc())
        ).all()
    )
    return [_issue_row_to_canonical(row) for row in rows]


def list_canonical_transitions(db: Session) -> list[CanonicalTransition]:
    """Return active canonical transitions for the current tenant session."""
    rows = list(
        db.scalars(
            select(IssueTransition)
            .where(IssueTransition.deleted_at.is_(None))
            .order_by(
                IssueTransition.external_key.asc(),
                IssueTransition.transitioned_at.asc(),
                IssueTransition.event_index.asc(),
            )
        ).all()
    )
    return [_transition_row_to_canonical(row) for row in rows]


def _issue_row_to_canonical(row: Issue) -> CanonicalIssue:
    return CanonicalIssue(
        external_key=row.external_key,
        project_key=row.project_key,
        summary=row.summary,
        status=row.status,
        issue_type=row.issue_type,
        acceptance_criteria=row.acceptance_criteria,
        story_points=row.story_points,
        created_at=row.source_created_at,
        updated_at=row.source_updated_at,
        epic_key=row.epic_key,
        description=row.description,
        team_key=row.team_key,
    )


def _transition_row_to_canonical(row: IssueTransition) -> CanonicalTransition:
    return CanonicalTransition(
        external_key=row.external_key,
        transitioned_at=row.transitioned_at,
        from_status=row.from_status,
        to_status=row.to_status,
        actor_id=row.actor_id,
        actor_display_name=row.actor_display_name,
        external_event_id=row.external_event_id,
        event_index=row.event_index,
    )
