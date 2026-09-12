"""Connector-agnostic issue and transition models (issue #15).

These types must not reference Jira field ids, payload keys, or client types.
Analytics and pipelines consume these shapes only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CanonicalIssue:
    """Normalized issue independent of any connector's field naming."""

    external_key: str
    project_key: str | None
    summary: str | None
    status: str | None
    issue_type: str | None
    acceptance_criteria: str | None
    story_points: float | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class CanonicalTransition:
    """Normalized status transition independent of connector payload shape."""

    external_key: str
    transitioned_at: datetime
    from_status: str | None
    to_status: str | None
    actor_id: str | None
    actor_display_name: str | None
    # Opaque connector event identity for idempotent upserts — not a field name.
    external_event_id: str
    event_index: int


@dataclass(frozen=True, slots=True)
class CanonicalFieldChange:
    """Normalized description/AC (or similar) field edit from changelog history.

    ``field`` uses connector-agnostic names (e.g. ``description``,
    ``acceptance_criteria``) — never Jira customfield ids. Event coordinates
    mirror ``CanonicalTransition`` for stable evidence refs (issue #19).
    """

    external_key: str
    changed_at: datetime
    field: str
    external_event_id: str
    event_index: int
