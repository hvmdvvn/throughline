"""Cycle time and time-in-status from canonical transitions (issue #17).

Results are stored on ``outcomes`` (not computed only at read time). Pure
computation lives in ``compute_cycle_time`` so tests can hand-construct
sequences without I/O; ``compute_and_store_outcomes_for_org`` persists them.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.analytics.status import StatusLane, classify_status
from throughline.db.models import IssueTransition, Outcome
from throughline.domain.issues import CanonicalTransition
from throughline.tenancy import require_current_org_id, skip_tenant_enforcement

UNKNOWN_STATUS_KEY = "<unknown>"


@dataclass(frozen=True, slots=True)
class CyclePass:
    """One completed in-progress → done interval."""

    started_at: datetime
    done_at: datetime
    duration_seconds: int


@dataclass(frozen=True, slots=True)
class CycleTimeResult:
    """Computed cycle metrics for a single issue."""

    external_key: str
    time_in_status_seconds: dict[str, int]
    cycle_time_seconds: int | None
    passes: tuple[CyclePass, ...]
    first_in_progress_at: datetime | None
    first_done_at: datetime | None
    last_done_at: datetime | None
    transitions_fingerprint: str


def transitions_fingerprint(transitions: Sequence[CanonicalTransition]) -> str:
    """Stable hash of a transition set (idempotency version for re-runs)."""
    lines: list[str] = []
    for t in _ordered(transitions):
        lines.append(
            "|".join(
                [
                    t.external_event_id,
                    str(t.event_index),
                    _aware(t.transitioned_at).isoformat(),
                    t.from_status or "",
                    t.to_status or "",
                ]
            )
        )
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest


def compute_cycle_time(
    transitions: Sequence[CanonicalTransition],
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> CycleTimeResult:
    """Compute time-in-status and cycle passes from a hand-built or stored sequence.

    Behaviour (covered by tests):

    - Transitions are ordered by ``(transitioned_at, event_index)``.
    - Time-in-status accumulates closed intervals between consecutive transitions
      (open-ended final status is not counted).
    - Negative intervals (should not occur after sort) clamp to zero seconds —
      never crash.
    - A cycle **pass** starts on entering an in-progress lane while no pass is
      open, and completes on the next enter-done. Moving backwards to todo while
      a pass is open does **not** cancel the pass (calendar time continues).
    - Reopen / multi-pass: each later in-progress→done after a done is a new pass.
    - ``cycle_time_seconds`` is the first completed pass duration (null if none).
    """
    classify_fn = classify or classify_status
    ordered = _ordered(transitions)
    if not ordered:
        raise ValueError("compute_cycle_time requires at least one transition")

    external_key = ordered[0].external_key
    if any(t.external_key != external_key for t in ordered):
        raise ValueError("all transitions must share the same external_key")

    time_in_status: dict[str, int] = defaultdict(int)
    for prev, curr in pairwise(ordered):
        status_key = prev.to_status if prev.to_status else UNKNOWN_STATUS_KEY
        delta = _seconds_between(prev.transitioned_at, curr.transitioned_at)
        time_in_status[status_key] += delta

    passes: list[CyclePass] = []
    pass_start: datetime | None = None
    first_in_progress_at: datetime | None = None
    first_done_at: datetime | None = None
    last_done_at: datetime | None = None

    for t in ordered:
        lane = classify_fn(t.to_status)
        at = _aware(t.transitioned_at)

        if lane == StatusLane.IN_PROGRESS and pass_start is None:
            pass_start = at
            if first_in_progress_at is None:
                first_in_progress_at = at

        if lane == StatusLane.DONE and pass_start is not None:
            duration = _seconds_between(pass_start, at)
            passes.append(CyclePass(started_at=pass_start, done_at=at, duration_seconds=duration))
            if first_done_at is None:
                first_done_at = at
            last_done_at = at
            pass_start = None
        elif lane == StatusLane.DONE:
            # Done without a prior open pass (e.g. created already done) — track
            # timestamps but do not invent a cycle start.
            if first_done_at is None:
                first_done_at = at
            last_done_at = at

    cycle_time = passes[0].duration_seconds if passes else None
    return CycleTimeResult(
        external_key=external_key,
        time_in_status_seconds=dict(time_in_status),
        cycle_time_seconds=cycle_time,
        passes=tuple(passes),
        first_in_progress_at=first_in_progress_at,
        first_done_at=first_done_at,
        last_done_at=last_done_at,
        transitions_fingerprint=transitions_fingerprint(ordered),
    )


def store_outcome(db: Session, result: CycleTimeResult) -> Outcome:
    """Upsert an ``outcomes`` row for the current org. Idempotent on fingerprint."""
    org_id = require_current_org_id()
    existing = db.scalar(select(Outcome).where(Outcome.external_key == result.external_key))
    if existing is None:
        existing = db.scalar(
            skip_tenant_enforcement(
                select(Outcome).where(
                    Outcome.org_id == org_id,
                    Outcome.external_key == result.external_key,
                )
            )
        )

    payload_passes = [
        {
            "started_at": p.started_at.isoformat(),
            "done_at": p.done_at.isoformat(),
            "duration_seconds": p.duration_seconds,
        }
        for p in result.passes
    ]

    if existing is not None:
        if (
            existing.deleted_at is None
            and existing.transitions_fingerprint == result.transitions_fingerprint
        ):
            return existing
        existing.deleted_at = None
        existing.time_in_status_seconds = result.time_in_status_seconds
        existing.cycle_time_seconds = result.cycle_time_seconds
        existing.pass_count = len(result.passes)
        existing.cycle_passes = payload_passes
        existing.first_in_progress_at = result.first_in_progress_at
        existing.first_done_at = result.first_done_at
        existing.last_done_at = result.last_done_at
        existing.transitions_fingerprint = result.transitions_fingerprint
        db.flush()
        return existing

    row = Outcome(
        org_id=org_id,
        external_key=result.external_key,
        time_in_status_seconds=result.time_in_status_seconds,
        cycle_time_seconds=result.cycle_time_seconds,
        pass_count=len(result.passes),
        cycle_passes=payload_passes,
        first_in_progress_at=result.first_in_progress_at,
        first_done_at=result.first_done_at,
        last_done_at=result.last_done_at,
        transitions_fingerprint=result.transitions_fingerprint,
    )
    db.add(row)
    db.flush()
    return row


def compute_and_store_outcomes_for_org(
    db: Session,
    org_id: uuid.UUID,
    *,
    classify: Callable[[str | None], StatusLane] | None = None,
) -> int:
    """Compute and persist outcomes for every issue with transitions in ``org_id``.

    Caller must already be in ``use_org(org_id)`` (or equivalent). Returns the
    number of issues for which an outcome was stored.
    """
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")

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

    count = 0
    for _key, transitions in sorted(by_key.items()):
        result = compute_cycle_time(transitions, classify=classify)
        store_outcome(db, result)
        count += 1
    db.flush()
    return count


def _ordered(transitions: Sequence[CanonicalTransition]) -> list[CanonicalTransition]:
    return sorted(
        transitions,
        key=lambda t: (_aware(t.transitioned_at), t.event_index, t.external_event_id),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _seconds_between(start: datetime, end: datetime) -> int:
    delta = _aware(end) - _aware(start)
    seconds = int(delta.total_seconds())
    return max(0, seconds)
