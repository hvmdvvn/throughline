"""Reopen / rework detection and aggregates (issue #18).

Detects done → active status transitions from canonical transitions, stores
each detection with a stable evidence link, and aggregates counts by epic,
project, and calendar month. Frequency only — no cause or blame labels.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.analytics.status import StatusLane, classify_status
from throughline.db.models import Issue, IssueTransition, ReopenAggregate, ReopenEvent
from throughline.domain.issues import CanonicalTransition
from throughline.tenancy import require_current_org_id, skip_tenant_enforcement

# Sentinel dimension key when epic/project is unknown (unique constraints disallow NULL).
UNASSIGNED_DIMENSION_KEY = ""

ACTIVE_LANES = frozenset({StatusLane.TODO, StatusLane.IN_PROGRESS})


class AggregateDimension(StrEnum):
    """Aggregation slice for stored reopen counts."""

    EPIC = "epic"
    PROJECT = "project"


@dataclass(frozen=True, slots=True)
class IssueDimensions:
    """Epic/project context for an issue at detection time."""

    project_key: str | None = None
    epic_key: str | None = None


@dataclass(frozen=True, slots=True)
class ReopenDetection:
    """One done → active transition with drill-down evidence coordinates."""

    external_key: str
    transitioned_at: datetime
    from_status: str | None
    to_status: str | None
    evidence_external_event_id: str
    evidence_event_index: int
    project_key: str | None
    epic_key: str | None

    @property
    def evidence_ref(self) -> str:
        """Stable evidence link for UI drill-down (#23/#25)."""
        return (
            f"issue:{self.external_key}/transition:"
            f"{self.evidence_external_event_id}:{self.evidence_event_index}"
        )


def is_reopen_transition(
    from_status: str | None,
    to_status: str | None,
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> bool:
    """True when ``from_status`` is done and ``to_status`` is todo or in-progress."""
    classify_fn = classify or classify_status
    return classify_fn(from_status) == StatusLane.DONE and classify_fn(to_status) in ACTIVE_LANES


def detect_reopens(
    transitions: Sequence[CanonicalTransition],
    *,
    dimensions: IssueDimensions | None = None,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> list[ReopenDetection]:
    """Return reopen detections for one issue's ordered transition sequence."""
    dims = dimensions or IssueDimensions()
    ordered = _ordered(transitions)
    if not ordered:
        return []

    external_key = ordered[0].external_key
    if any(t.external_key != external_key for t in ordered):
        raise ValueError("all transitions must share the same external_key")

    detections: list[ReopenDetection] = []
    for t in ordered:
        if not is_reopen_transition(t.from_status, t.to_status, classify=classify):
            continue
        detections.append(
            ReopenDetection(
                external_key=external_key,
                transitioned_at=_aware(t.transitioned_at),
                from_status=t.from_status,
                to_status=t.to_status,
                evidence_external_event_id=t.external_event_id,
                evidence_event_index=t.event_index,
                project_key=dims.project_key,
                epic_key=dims.epic_key,
            )
        )
    return detections


def month_period_start(when: datetime) -> date:
    """UTC calendar-month start for period aggregation."""
    at = _aware(when)
    return date(at.year, at.month, 1)


def aggregate_detections(
    detections: Sequence[ReopenDetection],
) -> list[tuple[date, AggregateDimension, str, int]]:
    """Count detections by (month_start, dimension, dimension_key)."""
    counts: dict[tuple[date, AggregateDimension, str], int] = defaultdict(int)
    for d in detections:
        period = month_period_start(d.transitioned_at)
        counts[(period, AggregateDimension.PROJECT, _dim_key(d.project_key))] += 1
        counts[(period, AggregateDimension.EPIC, _dim_key(d.epic_key))] += 1
    return [
        (period, dimension, key, count)
        for (period, dimension, key), count in sorted(
            counts.items(),
            key=lambda item: (item[0][0].isoformat(), item[0][1].value, item[0][2]),
        )
    ]


def store_reopen_detections(
    db: Session,
    detections: Sequence[ReopenDetection],
    *,
    replace_keys: Sequence[str] | None = None,
) -> list[ReopenEvent]:
    """Upsert reopen evidence rows. Idempotent on evidence coordinates.

    When ``replace_keys`` is set, soft-delete active events for those issue keys
    that are not present in ``detections`` (stale after transition edits).
    """
    org_id = require_current_org_id()
    keys = {d.external_key for d in detections}
    if replace_keys is not None:
        keys.update(replace_keys)

    kept: set[tuple[str, str, int]] = set()
    stored: list[ReopenEvent] = []

    for detection in detections:
        identity = (
            detection.external_key,
            detection.evidence_external_event_id,
            detection.evidence_event_index,
        )
        kept.add(identity)
        row = _upsert_event(db, org_id, detection)
        stored.append(row)

    if keys:
        existing = list(
            db.scalars(
                select(ReopenEvent).where(ReopenEvent.external_key.in_(sorted(keys)))
            ).all()
        )
        if not existing:
            existing = list(
                db.scalars(
                    skip_tenant_enforcement(
                        select(ReopenEvent).where(
                            ReopenEvent.org_id == org_id,
                            ReopenEvent.external_key.in_(sorted(keys)),
                            ReopenEvent.deleted_at.is_(None),
                        )
                    )
                ).all()
            )
        for row in existing:
            identity = (
                row.external_key,
                row.evidence_external_event_id,
                row.evidence_event_index,
            )
            if identity not in kept and row.deleted_at is None:
                row.deleted_at = datetime.now(UTC)

    db.flush()
    return stored


def rebuild_reopen_aggregates(db: Session) -> int:
    """Rebuild month x epic and month x project aggregates from active events.

    Idempotent: upserts counts and soft-deletes aggregate rows that fall to zero.
    Returns the number of active aggregate rows after rebuild.
    """
    org_id = require_current_org_id()
    events = list(db.scalars(select(ReopenEvent)).all())
    detections = [
        ReopenDetection(
            external_key=e.external_key,
            transitioned_at=e.transitioned_at,
            from_status=e.from_status,
            to_status=e.to_status,
            evidence_external_event_id=e.evidence_external_event_id,
            evidence_event_index=e.evidence_event_index,
            project_key=e.project_key or None,
            epic_key=e.epic_key or None,
        )
        for e in events
    ]
    desired = {
        (period, dimension.value, key): count
        for period, dimension, key, count in aggregate_detections(detections)
    }

    existing = list(db.scalars(select(ReopenAggregate)).all())
    if not existing:
        existing = list(
            db.scalars(
                skip_tenant_enforcement(
                    select(ReopenAggregate).where(
                        ReopenAggregate.org_id == org_id,
                        ReopenAggregate.deleted_at.is_(None),
                    )
                )
            ).all()
        )

    by_identity = {
        (row.period_start, row.dimension, row.dimension_key): row for row in existing
    }

    for (period, dimension, key), count in desired.items():
        row = by_identity.pop((period, dimension, key), None)
        if row is None:
            db.add(
                ReopenAggregate(
                    org_id=org_id,
                    period_start=period,
                    period_grain="month",
                    dimension=dimension,
                    dimension_key=key,
                    reopen_count=count,
                )
            )
        else:
            row.deleted_at = None
            row.period_grain = "month"
            row.reopen_count = count

    for row in by_identity.values():
        if row.deleted_at is None:
            row.deleted_at = datetime.now(UTC)

    db.flush()
    return len(desired)


def compute_and_store_reopens_for_org(
    db: Session,
    org_id: uuid.UUID,
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
    dimensions_by_key: Mapping[str, IssueDimensions] | None = None,
) -> tuple[int, int]:
    """Detect, store, and aggregate reopens for every issue with transitions.

    Caller must already be in ``use_org(org_id)``. Returns
    ``(detection_count, aggregate_count)``.
    """
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")

    dims_map = dict(dimensions_by_key or {})
    if not dims_map:
        dims_map = _load_issue_dimensions(db)

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
    by_key: dict[str, list[CanonicalTransition]] = defaultdict(list)
    for row in rows:
        by_key[row.external_key].append(
            CanonicalTransition(
                external_key=row.external_key,
                transitioned_at=row.transitioned_at,
                from_status=row.from_status,
                to_status=row.to_status,
                actor_id=row.actor_id,
                actor_display_name=row.actor_display_name,
                external_event_id=row.external_event_id,
                event_index=row.event_index,
            )
        )

    all_detections: list[ReopenDetection] = []
    for key, transitions in sorted(by_key.items()):
        dims = dims_map.get(key, IssueDimensions())
        all_detections.extend(
            detect_reopens(transitions, dimensions=dims, classify=classify)
        )

    store_reopen_detections(
        db,
        all_detections,
        replace_keys=sorted(by_key.keys()),
    )
    aggregate_count = rebuild_reopen_aggregates(db)
    return len(all_detections), aggregate_count


def _load_issue_dimensions(db: Session) -> dict[str, IssueDimensions]:
    issues = list(db.scalars(select(Issue).where(Issue.deleted_at.is_(None))).all())
    return {
        issue.external_key: IssueDimensions(
            project_key=issue.project_key,
            epic_key=issue.epic_key,
        )
        for issue in issues
    }


def _upsert_event(db: Session, org_id: uuid.UUID, detection: ReopenDetection) -> ReopenEvent:
    existing = db.scalar(
        select(ReopenEvent).where(
            ReopenEvent.external_key == detection.external_key,
            ReopenEvent.evidence_external_event_id == detection.evidence_external_event_id,
            ReopenEvent.evidence_event_index == detection.evidence_event_index,
        )
    )
    if existing is None:
        existing = db.scalar(
            skip_tenant_enforcement(
                select(ReopenEvent).where(
                    ReopenEvent.org_id == org_id,
                    ReopenEvent.external_key == detection.external_key,
                    ReopenEvent.evidence_external_event_id
                    == detection.evidence_external_event_id,
                    ReopenEvent.evidence_event_index == detection.evidence_event_index,
                )
            )
        )

    project_key = _dim_key(detection.project_key)
    epic_key = _dim_key(detection.epic_key)

    if existing is not None:
        existing.deleted_at = None
        existing.transitioned_at = detection.transitioned_at
        existing.from_status = detection.from_status
        existing.to_status = detection.to_status
        existing.project_key = project_key
        existing.epic_key = epic_key
        existing.evidence_ref = detection.evidence_ref
        db.flush()
        return existing

    row = ReopenEvent(
        org_id=org_id,
        external_key=detection.external_key,
        project_key=project_key,
        epic_key=epic_key,
        transitioned_at=detection.transitioned_at,
        from_status=detection.from_status,
        to_status=detection.to_status,
        evidence_external_event_id=detection.evidence_external_event_id,
        evidence_event_index=detection.evidence_event_index,
        evidence_ref=detection.evidence_ref,
    )
    db.add(row)
    db.flush()
    return row


def _dim_key(value: str | None) -> str:
    if value is None:
        return UNASSIGNED_DIMENSION_KEY
    stripped = value.strip()
    return stripped if stripped else UNASSIGNED_DIMENSION_KEY


def _ordered(transitions: Sequence[CanonicalTransition]) -> list[CanonicalTransition]:
    return sorted(
        transitions,
        key=lambda t: (_aware(t.transitioned_at), t.event_index, t.external_event_id),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
