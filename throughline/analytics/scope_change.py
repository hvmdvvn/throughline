"""Scope change metrics: late epic children + post-start spec edits (issue #19).

Two proxy signals for spec instability (not value judgments):

1. **Late children** — child issues created after the epic's first child entered
   in-progress.
2. **Spec changes** — description or acceptance-criteria edits after an issue's
   own first in-progress transition.

Results are stored with evidence refs for drill-down. Empty / never-started
epics yield defined zero results without errors.
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

from throughline.analytics.reopen import AggregateDimension, IssueDimensions, month_period_start
from throughline.analytics.status import StatusLane, classify_status
from throughline.db.models import (
    Issue,
    IssueFieldChange,
    IssueTransition,
    LateChildEvent,
    ScopeChangeAggregate,
    SpecChangeEvent,
)
from throughline.domain.issues import CanonicalFieldChange, CanonicalTransition
from throughline.tenancy import require_current_org_id, skip_tenant_enforcement

UNASSIGNED_DIMENSION_KEY = ""

SPEC_FIELDS = frozenset({"description", "acceptance_criteria"})


class SpecField(StrEnum):
    """Canonical field names counted as spec-instability edits."""

    DESCRIPTION = "description"
    ACCEPTANCE_CRITERIA = "acceptance_criteria"


@dataclass(frozen=True, slots=True)
class EpicChild:
    """One issue under an epic for late-scope detection."""

    external_key: str
    epic_key: str
    created_at: datetime | None
    project_key: str | None = None


@dataclass(frozen=True, slots=True)
class LateChildDetection:
    """Child created after epic start, with timing and evidence."""

    epic_key: str
    external_key: str
    child_created_at: datetime
    epic_started_at: datetime
    delay_seconds: int
    project_key: str | None

    @property
    def evidence_ref(self) -> str:
        return f"epic:{self.epic_key}/late-child:{self.external_key}"


@dataclass(frozen=True, slots=True)
class SpecChangeDetection:
    """Description/AC edit after the issue's work began."""

    external_key: str
    field: str
    changed_at: datetime
    work_began_at: datetime
    evidence_external_event_id: str
    evidence_event_index: int
    project_key: str | None
    epic_key: str | None

    @property
    def evidence_ref(self) -> str:
        return (
            f"issue:{self.external_key}/field:{self.field}:"
            f"{self.evidence_external_event_id}:{self.evidence_event_index}"
        )


@dataclass(frozen=True, slots=True)
class EpicScopeResult:
    """Per-epic late-child summary (empty/zero when no start or no children)."""

    epic_key: str
    epic_started_at: datetime | None
    child_count: int
    late_children: tuple[LateChildDetection, ...]

    @property
    def late_child_count(self) -> int:
        return len(self.late_children)


def first_in_progress_at(
    transitions: Sequence[CanonicalTransition],
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> datetime | None:
    """Earliest transition that enters an in-progress lane for one issue."""
    classify_fn = classify or classify_status
    first: datetime | None = None
    for t in _ordered_transitions(transitions):
        if classify_fn(t.to_status) != StatusLane.IN_PROGRESS:
            continue
        at = _aware(t.transitioned_at)
        if first is None or at < first:
            first = at
    return first


def epic_started_at(
    children_transitions: Mapping[str, Sequence[CanonicalTransition]],
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> datetime | None:
    """When the epic's first child entered in-progress; None if never started."""
    started: datetime | None = None
    for transitions in children_transitions.values():
        child_start = first_in_progress_at(transitions, classify=classify)
        if child_start is None:
            continue
        if started is None or child_start < started:
            started = child_start
    return started


def detect_late_children(
    children: Sequence[EpicChild],
    started_at: datetime | None,
) -> list[LateChildDetection]:
    """Children with ``created_at`` strictly after ``started_at``.

    Returns ``[]`` when the epic never started, has no children, or every child
    was created on/before start — never raises on empty input.
    """
    if started_at is None or not children:
        return []

    start = _aware(started_at)
    late: list[LateChildDetection] = []
    for child in children:
        if child.created_at is None:
            continue
        created = _aware(child.created_at)
        if created <= start:
            continue
        delay = int((created - start).total_seconds())
        late.append(
            LateChildDetection(
                epic_key=child.epic_key,
                external_key=child.external_key,
                child_created_at=created,
                epic_started_at=start,
                delay_seconds=delay,
                project_key=child.project_key,
            )
        )
    late.sort(key=lambda d: (d.child_created_at, d.external_key))
    return late


def measure_epic_scope(
    epic_key: str,
    children: Sequence[EpicChild],
    children_transitions: Mapping[str, Sequence[CanonicalTransition]],
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> EpicScopeResult:
    """Compute late-child detections for one epic (defined empty when idle)."""
    keyed = [c for c in children if c.epic_key == epic_key]
    started = epic_started_at(children_transitions, classify=classify)
    late = detect_late_children(keyed, started)
    return EpicScopeResult(
        epic_key=epic_key,
        epic_started_at=started,
        child_count=len(keyed),
        late_children=tuple(late),
    )


def detect_spec_changes(
    field_changes: Sequence[CanonicalFieldChange],
    work_began_at: datetime | None,
    *,
    dimensions: IssueDimensions | None = None,
) -> list[SpecChangeDetection]:
    """Description/AC edits strictly after ``work_began_at`` for one issue.

    Returns ``[]`` when work never began or there are no qualifying edits.
    """
    if work_began_at is None or not field_changes:
        return []

    dims = dimensions or IssueDimensions()
    began = _aware(work_began_at)
    detections: list[SpecChangeDetection] = []
    for change in _ordered_field_changes(field_changes):
        if change.field not in SPEC_FIELDS:
            continue
        at = _aware(change.changed_at)
        if at <= began:
            continue
        detections.append(
            SpecChangeDetection(
                external_key=change.external_key,
                field=change.field,
                changed_at=at,
                work_began_at=began,
                evidence_external_event_id=change.external_event_id,
                evidence_event_index=change.event_index,
                project_key=dims.project_key,
                epic_key=dims.epic_key,
            )
        )
    return detections


def aggregate_scope_signals(
    late_children: Sequence[LateChildDetection],
    spec_changes: Sequence[SpecChangeDetection],
) -> list[tuple[date, AggregateDimension, str, int, int]]:
    """Roll up volume by (month_start, dimension, key) → (late, spec) counts."""
    late_counts: dict[tuple[date, AggregateDimension, str], int] = defaultdict(int)
    spec_counts: dict[tuple[date, AggregateDimension, str], int] = defaultdict(int)

    for d in late_children:
        period = month_period_start(d.child_created_at)
        late_counts[(period, AggregateDimension.PROJECT, _dim_key(d.project_key))] += 1
        late_counts[(period, AggregateDimension.EPIC, _dim_key(d.epic_key))] += 1

    for d in spec_changes:
        period = month_period_start(d.changed_at)
        spec_counts[(period, AggregateDimension.PROJECT, _dim_key(d.project_key))] += 1
        spec_counts[(period, AggregateDimension.EPIC, _dim_key(d.epic_key))] += 1

    keys = sorted(
        set(late_counts) | set(spec_counts),
        key=lambda item: (item[0].isoformat(), item[1].value, item[2]),
    )
    return [
        (
            period,
            dimension,
            key,
            late_counts.get((period, dimension, key), 0),
            spec_counts.get((period, dimension, key), 0),
        )
        for period, dimension, key in keys
    ]


def store_late_child_detections(
    db: Session,
    detections: Sequence[LateChildDetection],
    *,
    replace_epic_keys: Sequence[str] | None = None,
) -> list[LateChildEvent]:
    """Upsert late-child evidence rows. Idempotent on (epic, child)."""
    org_id = require_current_org_id()
    epic_keys = {d.epic_key for d in detections}
    if replace_epic_keys is not None:
        epic_keys.update(replace_epic_keys)

    kept: set[tuple[str, str]] = set()
    stored: list[LateChildEvent] = []
    for detection in detections:
        kept.add((detection.epic_key, detection.external_key))
        stored.append(_upsert_late_child(db, org_id, detection))

    if epic_keys:
        existing = _list_late_children_for_epics(db, org_id, sorted(epic_keys))
        for row in existing:
            if (row.epic_key, row.external_key) not in kept and row.deleted_at is None:
                row.deleted_at = datetime.now(UTC)

    db.flush()
    return stored


def store_spec_change_detections(
    db: Session,
    detections: Sequence[SpecChangeDetection],
    *,
    replace_keys: Sequence[str] | None = None,
) -> list[SpecChangeEvent]:
    """Upsert spec-change evidence rows. Idempotent on evidence coordinates."""
    org_id = require_current_org_id()
    keys = {d.external_key for d in detections}
    if replace_keys is not None:
        keys.update(replace_keys)

    kept: set[tuple[str, str, int]] = set()
    stored: list[SpecChangeEvent] = []
    for detection in detections:
        identity = (
            detection.external_key,
            detection.evidence_external_event_id,
            detection.evidence_event_index,
        )
        kept.add(identity)
        stored.append(_upsert_spec_change(db, org_id, detection))

    if keys:
        existing = _list_spec_changes_for_keys(db, org_id, sorted(keys))
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


def rebuild_scope_change_aggregates(db: Session) -> int:
    """Rebuild month x epic and month x project aggregates from active events."""
    org_id = require_current_org_id()
    late_rows = list(db.scalars(select(LateChildEvent)).all())
    spec_rows = list(db.scalars(select(SpecChangeEvent)).all())

    late = [
        LateChildDetection(
            epic_key=r.epic_key,
            external_key=r.external_key,
            child_created_at=r.child_created_at,
            epic_started_at=r.epic_started_at,
            delay_seconds=int(r.delay_seconds),
            project_key=r.project_key or None,
        )
        for r in late_rows
    ]
    specs = [
        SpecChangeDetection(
            external_key=r.external_key,
            field=r.field,
            changed_at=r.changed_at,
            work_began_at=r.work_began_at,
            evidence_external_event_id=r.evidence_external_event_id,
            evidence_event_index=r.evidence_event_index,
            project_key=r.project_key or None,
            epic_key=r.epic_key or None,
        )
        for r in spec_rows
    ]
    desired = {
        (period, dimension.value, key): (late_n, spec_n)
        for period, dimension, key, late_n, spec_n in aggregate_scope_signals(late, specs)
    }

    existing = list(db.scalars(select(ScopeChangeAggregate)).all())
    if not existing:
        existing = list(
            db.scalars(
                skip_tenant_enforcement(
                    select(ScopeChangeAggregate).where(
                        ScopeChangeAggregate.org_id == org_id,
                        ScopeChangeAggregate.deleted_at.is_(None),
                    )
                )
            ).all()
        )

    by_identity = {
        (row.period_start, row.dimension, row.dimension_key): row for row in existing
    }

    for (period, dimension, key), (late_n, spec_n) in desired.items():
        row = by_identity.pop((period, dimension, key), None)
        if row is None:
            db.add(
                ScopeChangeAggregate(
                    org_id=org_id,
                    period_start=period,
                    period_grain="month",
                    dimension=dimension,
                    dimension_key=key,
                    late_child_count=late_n,
                    spec_change_count=spec_n,
                )
            )
        else:
            row.deleted_at = None
            row.period_grain = "month"
            row.late_child_count = late_n
            row.spec_change_count = spec_n

    for row in by_identity.values():
        if row.deleted_at is None:
            row.deleted_at = datetime.now(UTC)

    db.flush()
    return len(desired)


def compute_and_store_scope_change_for_org(
    db: Session,
    org_id: uuid.UUID,
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> tuple[int, int, int]:
    """Detect, store, and aggregate both scope signals for the current org.

    Caller must already be in ``use_org(org_id)``. Returns
    ``(late_child_count, spec_change_count, aggregate_count)``.
    """
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")

    issues = list(db.scalars(select(Issue).where(Issue.deleted_at.is_(None))).all())
    transitions = list(
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
    field_rows = list(
        db.scalars(
            select(IssueFieldChange)
            .where(IssueFieldChange.deleted_at.is_(None))
            .order_by(
                IssueFieldChange.external_key.asc(),
                IssueFieldChange.changed_at.asc(),
                IssueFieldChange.event_index.asc(),
            )
        ).all()
    )

    by_key_transitions: dict[str, list[CanonicalTransition]] = defaultdict(list)
    for row in transitions:
        by_key_transitions[row.external_key].append(
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

    by_key_fields: dict[str, list[CanonicalFieldChange]] = defaultdict(list)
    for row in field_rows:
        by_key_fields[row.external_key].append(
            CanonicalFieldChange(
                external_key=row.external_key,
                changed_at=row.changed_at,
                field=row.field,
                external_event_id=row.external_event_id,
                event_index=row.event_index,
            )
        )

    children_by_epic: dict[str, list[EpicChild]] = defaultdict(list)
    dims_by_key: dict[str, IssueDimensions] = {}
    for issue in issues:
        dims_by_key[issue.external_key] = IssueDimensions(
            project_key=issue.project_key,
            epic_key=issue.epic_key,
        )
        if issue.epic_key:
            children_by_epic[issue.epic_key].append(
                EpicChild(
                    external_key=issue.external_key,
                    epic_key=issue.epic_key,
                    created_at=issue.source_created_at,
                    project_key=issue.project_key,
                )
            )

    all_late: list[LateChildDetection] = []
    for epic_key, children in sorted(children_by_epic.items()):
        child_transitions = {
            c.external_key: by_key_transitions.get(c.external_key, []) for c in children
        }
        result = measure_epic_scope(
            epic_key,
            children,
            child_transitions,
            classify=classify,
        )
        all_late.extend(result.late_children)

    # Epics with no children: still "measured" as empty — nothing to store.
    all_specs: list[SpecChangeDetection] = []
    for key, changes in sorted(by_key_fields.items()):
        began = first_in_progress_at(
            by_key_transitions.get(key, []),
            classify=classify,
        )
        all_specs.extend(
            detect_spec_changes(
                changes,
                began,
                dimensions=dims_by_key.get(key, IssueDimensions()),
            )
        )

    store_late_child_detections(
        db,
        all_late,
        replace_epic_keys=sorted(children_by_epic.keys()),
    )
    store_spec_change_detections(
        db,
        all_specs,
        replace_keys=sorted({*by_key_fields.keys(), *by_key_transitions.keys()}),
    )
    aggregate_count = rebuild_scope_change_aggregates(db)
    return len(all_late), len(all_specs), aggregate_count


def _upsert_late_child(
    db: Session, org_id: uuid.UUID, detection: LateChildDetection
) -> LateChildEvent:
    existing = db.scalar(
        select(LateChildEvent).where(
            LateChildEvent.epic_key == detection.epic_key,
            LateChildEvent.external_key == detection.external_key,
        )
    )
    if existing is None:
        existing = db.scalar(
            skip_tenant_enforcement(
                select(LateChildEvent).where(
                    LateChildEvent.org_id == org_id,
                    LateChildEvent.epic_key == detection.epic_key,
                    LateChildEvent.external_key == detection.external_key,
                )
            )
        )

    project_key = _dim_key(detection.project_key)
    if existing is not None:
        existing.deleted_at = None
        existing.project_key = project_key
        existing.child_created_at = detection.child_created_at
        existing.epic_started_at = detection.epic_started_at
        existing.delay_seconds = detection.delay_seconds
        existing.evidence_ref = detection.evidence_ref
        db.flush()
        return existing

    row = LateChildEvent(
        org_id=org_id,
        epic_key=detection.epic_key,
        external_key=detection.external_key,
        project_key=project_key,
        child_created_at=detection.child_created_at,
        epic_started_at=detection.epic_started_at,
        delay_seconds=detection.delay_seconds,
        evidence_ref=detection.evidence_ref,
    )
    db.add(row)
    db.flush()
    return row


def _upsert_spec_change(
    db: Session, org_id: uuid.UUID, detection: SpecChangeDetection
) -> SpecChangeEvent:
    existing = db.scalar(
        select(SpecChangeEvent).where(
            SpecChangeEvent.external_key == detection.external_key,
            SpecChangeEvent.evidence_external_event_id
            == detection.evidence_external_event_id,
            SpecChangeEvent.evidence_event_index == detection.evidence_event_index,
        )
    )
    if existing is None:
        existing = db.scalar(
            skip_tenant_enforcement(
                select(SpecChangeEvent).where(
                    SpecChangeEvent.org_id == org_id,
                    SpecChangeEvent.external_key == detection.external_key,
                    SpecChangeEvent.evidence_external_event_id
                    == detection.evidence_external_event_id,
                    SpecChangeEvent.evidence_event_index
                    == detection.evidence_event_index,
                )
            )
        )

    project_key = _dim_key(detection.project_key)
    epic_key = _dim_key(detection.epic_key)
    if existing is not None:
        existing.deleted_at = None
        existing.project_key = project_key
        existing.epic_key = epic_key
        existing.field = detection.field
        existing.changed_at = detection.changed_at
        existing.work_began_at = detection.work_began_at
        existing.evidence_ref = detection.evidence_ref
        db.flush()
        return existing

    row = SpecChangeEvent(
        org_id=org_id,
        external_key=detection.external_key,
        project_key=project_key,
        epic_key=epic_key,
        field=detection.field,
        changed_at=detection.changed_at,
        work_began_at=detection.work_began_at,
        evidence_external_event_id=detection.evidence_external_event_id,
        evidence_event_index=detection.evidence_event_index,
        evidence_ref=detection.evidence_ref,
    )
    db.add(row)
    db.flush()
    return row


def _list_late_children_for_epics(
    db: Session, org_id: uuid.UUID, epic_keys: Sequence[str]
) -> list[LateChildEvent]:
    existing = list(
        db.scalars(select(LateChildEvent).where(LateChildEvent.epic_key.in_(epic_keys))).all()
    )
    if existing:
        return existing
    return list(
        db.scalars(
            skip_tenant_enforcement(
                select(LateChildEvent).where(
                    LateChildEvent.org_id == org_id,
                    LateChildEvent.epic_key.in_(list(epic_keys)),
                    LateChildEvent.deleted_at.is_(None),
                )
            )
        ).all()
    )


def _list_spec_changes_for_keys(
    db: Session, org_id: uuid.UUID, keys: Sequence[str]
) -> list[SpecChangeEvent]:
    existing = list(
        db.scalars(select(SpecChangeEvent).where(SpecChangeEvent.external_key.in_(keys))).all()
    )
    if existing:
        return existing
    return list(
        db.scalars(
            skip_tenant_enforcement(
                select(SpecChangeEvent).where(
                    SpecChangeEvent.org_id == org_id,
                    SpecChangeEvent.external_key.in_(list(keys)),
                    SpecChangeEvent.deleted_at.is_(None),
                )
            )
        ).all()
    )


def _dim_key(value: str | None) -> str:
    if value is None:
        return UNASSIGNED_DIMENSION_KEY
    stripped = value.strip()
    return stripped if stripped else UNASSIGNED_DIMENSION_KEY


def _ordered_transitions(
    transitions: Sequence[CanonicalTransition],
) -> list[CanonicalTransition]:
    return sorted(
        transitions,
        key=lambda t: (_aware(t.transitioned_at), t.event_index, t.external_event_id),
    )


def _ordered_field_changes(
    changes: Sequence[CanonicalFieldChange],
) -> list[CanonicalFieldChange]:
    return sorted(
        changes,
        key=lambda c: (_aware(c.changed_at), c.event_index, c.external_event_id),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
