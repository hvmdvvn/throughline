"""Estimation accuracy: estimate vs measured cycle time (issue #21).

Compares story points or original estimates against stored cycle-time outcomes
(#17). Aggregates **per team only** with coverage alongside accuracy.
Missing or inconsistent estimates reduce coverage — they never crash the job.
Never computes or stores per-person figures.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.analytics.reopen import month_period_start
from throughline.db.models import (
    EstimationAccuracyAggregate,
    EstimationAccuracyIssueMetric,
    Issue,
    Outcome,
)
from throughline.domain.issues import CanonicalIssue
from throughline.tenancy import require_current_org_id, skip_tenant_enforcement

UNASSIGNED_DIMENSION_KEY = ""

# actual/estimate below this → estimate was too high (over-estimation).
OVER_ESTIMATE_MAX_RATIO = 0.75
# actual/estimate above this → estimate was too low (under-estimation).
UNDER_ESTIMATE_MIN_RATIO = 1.25


class EstimateKind(StrEnum):
    """Which estimate source was used for the comparison."""

    NONE = "none"
    ORIGINAL_ESTIMATE = "original_estimate"
    STORY_POINTS = "story_points"


class AccuracyBucket(StrEnum):
    """Over / under / accurate classification — not a person score."""

    OVER = "over"
    UNDER = "under"
    ACCURATE = "accurate"
    UNCOMPARABLE = "uncomparable"


class EstimationAccuracyDimension(StrEnum):
    """Aggregation slices — team only (never per-person)."""

    TEAM = "team"


@dataclass(frozen=True, slots=True)
class EstimationAccuracyIssueDetection:
    """One issue's estimate-vs-actual comparison with drill-down evidence."""

    external_key: str
    team_key: str | None
    story_points: float | None
    original_estimate_seconds: int | None
    cycle_time_seconds: int | None
    has_usable_estimate: bool
    estimate_kind: EstimateKind
    estimated_seconds: float | None
    accuracy_ratio: float | None
    accuracy_bucket: AccuracyBucket
    seconds_per_point: float | None
    observed_at: datetime

    @property
    def evidence_ref(self) -> str:
        return f"issue:{self.external_key}/estimation-accuracy"


@dataclass(frozen=True, slots=True)
class EstimationAccuracyAggregateRow:
    """One team x month rollup of coverage and over/under counts."""

    period_start: date
    dimension: EstimationAccuracyDimension
    dimension_key: str
    issue_count: int
    with_estimate_count: int
    comparable_count: int
    coverage: float
    over_count: int
    under_count: int
    accurate_count: int
    accuracy_ratio_sum: float
    evidence_issue_keys: tuple[str, ...]


def has_usable_estimate(
    story_points: float | None,
    original_estimate_seconds: int | None,
) -> bool:
    """True when story points or original estimate is present and positive."""
    if original_estimate_seconds is not None and original_estimate_seconds > 0:
        return True
    return story_points is not None and story_points > 0


def accuracy_bucket_for_ratio(ratio: float | None) -> AccuracyBucket:
    """Map actual/estimate ratio to over / under / accurate / uncomparable."""
    if ratio is None:
        return AccuracyBucket.UNCOMPARABLE
    if ratio < OVER_ESTIMATE_MAX_RATIO:
        return AccuracyBucket.OVER
    if ratio > UNDER_ESTIMATE_MIN_RATIO:
        return AccuracyBucket.UNDER
    return AccuracyBucket.ACCURATE


def median_seconds_per_point(
    pairs: Sequence[tuple[float, int]],
) -> float | None:
    """Median cycle_time/story_points for positive pairs; ``None`` if empty."""
    rates: list[float] = []
    for points, cycle_seconds in pairs:
        if points is None or cycle_seconds is None:
            continue
        try:
            sp = float(points)
            ct = int(cycle_seconds)
        except (TypeError, ValueError):
            continue
        if sp <= 0 or ct <= 0:
            continue
        rates.append(ct / sp)
    if not rates:
        return None
    return float(statistics.median(rates))


def team_seconds_per_point_map(
    issues: Sequence[CanonicalIssue],
    cycle_times: Mapping[str, int | None],
) -> dict[str, float]:
    """Per-team median seconds/point from issues that have both SP and cycle time."""
    by_team: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for issue in issues:
        ct = cycle_times.get(issue.external_key)
        sp = issue.story_points
        if sp is None or ct is None:
            continue
        try:
            points = float(sp)
            seconds = int(ct)
        except (TypeError, ValueError):
            continue
        if points <= 0 or seconds <= 0:
            continue
        by_team[_dim_key(issue.team_key)].append((points, seconds))
    return {
        team: rate
        for team, pairs in by_team.items()
        if (rate := median_seconds_per_point(pairs)) is not None
    }


def resolve_estimated_seconds(
    *,
    story_points: float | None,
    original_estimate_seconds: int | None,
    seconds_per_point: float | None,
) -> tuple[float | None, EstimateKind]:
    """Prefer original estimate (seconds); else SP x team seconds/point."""
    if original_estimate_seconds is not None and original_estimate_seconds > 0:
        return float(original_estimate_seconds), EstimateKind.ORIGINAL_ESTIMATE
    if story_points is not None and story_points > 0:
        if seconds_per_point is not None and seconds_per_point > 0:
            return float(story_points) * float(seconds_per_point), EstimateKind.STORY_POINTS
        return None, EstimateKind.STORY_POINTS
    return None, EstimateKind.NONE


def detect_estimation_accuracy_for_issue(
    issue: CanonicalIssue,
    *,
    cycle_time_seconds: int | None,
    seconds_per_point: float | None = None,
    observed_at: datetime | None = None,
) -> EstimationAccuracyIssueDetection:
    """Compare one issue's estimate to measured cycle time.

    Missing estimates or cycle time yield ``uncomparable`` without raising.
    """
    usable = has_usable_estimate(issue.story_points, issue.original_estimate_seconds)
    estimated, kind = resolve_estimated_seconds(
        story_points=issue.story_points,
        original_estimate_seconds=issue.original_estimate_seconds,
        seconds_per_point=seconds_per_point,
    )

    ratio: float | None = None
    ct: int | None = None
    if cycle_time_seconds is not None:
        try:
            ct = int(cycle_time_seconds)
        except (TypeError, ValueError):
            ct = None
        if ct is not None and ct <= 0:
            ct = None

    if estimated is not None and estimated > 0 and ct is not None:
        ratio = ct / estimated

    when = observed_at
    if when is None:
        when = issue.updated_at or issue.created_at or datetime.now(UTC)

    return EstimationAccuracyIssueDetection(
        external_key=issue.external_key,
        team_key=issue.team_key,
        story_points=issue.story_points,
        original_estimate_seconds=issue.original_estimate_seconds,
        cycle_time_seconds=ct,
        has_usable_estimate=usable,
        estimate_kind=kind if usable else EstimateKind.NONE,
        estimated_seconds=estimated,
        accuracy_ratio=ratio,
        accuracy_bucket=accuracy_bucket_for_ratio(ratio),
        seconds_per_point=seconds_per_point if kind == EstimateKind.STORY_POINTS else None,
        observed_at=_aware(when),
    )


def detect_estimation_accuracy(
    issues: Sequence[CanonicalIssue],
    cycle_times: Mapping[str, int | None],
    *,
    observed_at_by_key: Mapping[str, datetime] | None = None,
) -> list[EstimationAccuracyIssueDetection]:
    """Compute per-issue detections with team-level story-point calibration."""
    spp_by_team = team_seconds_per_point_map(issues, cycle_times)
    observed = observed_at_by_key or {}
    detections: list[EstimationAccuracyIssueDetection] = []
    for issue in issues:
        team = _dim_key(issue.team_key)
        detections.append(
            detect_estimation_accuracy_for_issue(
                issue,
                cycle_time_seconds=cycle_times.get(issue.external_key),
                seconds_per_point=spp_by_team.get(team),
                observed_at=observed.get(issue.external_key),
            )
        )
    return detections


def aggregate_estimation_accuracy(
    detections: Sequence[EstimationAccuracyIssueDetection],
) -> list[EstimationAccuracyAggregateRow]:
    """Roll up coverage and over/under counts by calendar month x team."""

    @dataclass
    class _Bucket:
        issue_count: int = 0
        with_estimate_count: int = 0
        comparable_count: int = 0
        over_count: int = 0
        under_count: int = 0
        accurate_count: int = 0
        accuracy_ratio_sum: float = 0.0
        evidence_issue_keys: list[str] | None = None

        def __post_init__(self) -> None:
            if self.evidence_issue_keys is None:
                self.evidence_issue_keys = []

    buckets: dict[tuple[date, str], _Bucket] = defaultdict(_Bucket)

    for d in detections:
        period = month_period_start(d.observed_at)
        key = _dim_key(d.team_key)
        b = buckets[(period, key)]
        b.issue_count += 1
        if d.has_usable_estimate:
            b.with_estimate_count += 1
        if d.accuracy_bucket == AccuracyBucket.OVER:
            b.over_count += 1
            b.comparable_count += 1
            assert d.accuracy_ratio is not None
            b.accuracy_ratio_sum += d.accuracy_ratio
        elif d.accuracy_bucket == AccuracyBucket.UNDER:
            b.under_count += 1
            b.comparable_count += 1
            assert d.accuracy_ratio is not None
            b.accuracy_ratio_sum += d.accuracy_ratio
        elif d.accuracy_bucket == AccuracyBucket.ACCURATE:
            b.accurate_count += 1
            b.comparable_count += 1
            assert d.accuracy_ratio is not None
            b.accuracy_ratio_sum += d.accuracy_ratio
        assert b.evidence_issue_keys is not None
        b.evidence_issue_keys.append(d.external_key)

    rows: list[EstimationAccuracyAggregateRow] = []
    for (period, key), b in sorted(
        buckets.items(),
        key=lambda item: (item[0][0].isoformat(), item[0][1]),
    ):
        assert b.evidence_issue_keys is not None
        coverage = (
            b.with_estimate_count / b.issue_count if b.issue_count > 0 else 0.0
        )
        rows.append(
            EstimationAccuracyAggregateRow(
                period_start=period,
                dimension=EstimationAccuracyDimension.TEAM,
                dimension_key=key,
                issue_count=b.issue_count,
                with_estimate_count=b.with_estimate_count,
                comparable_count=b.comparable_count,
                coverage=coverage,
                over_count=b.over_count,
                under_count=b.under_count,
                accurate_count=b.accurate_count,
                accuracy_ratio_sum=b.accuracy_ratio_sum,
                evidence_issue_keys=tuple(sorted(set(b.evidence_issue_keys))),
            )
        )
    return rows


def store_estimation_accuracy_metrics(
    db: Session,
    detections: Sequence[EstimationAccuracyIssueDetection],
    *,
    replace_keys: Sequence[str] | None = None,
) -> list[EstimationAccuracyIssueMetric]:
    """Upsert per-issue metrics. Idempotent on ``(org_id, external_key)``."""
    org_id = require_current_org_id()
    keys = {d.external_key for d in detections}
    if replace_keys is not None:
        keys.update(replace_keys)

    by_key: dict[str, EstimationAccuracyIssueMetric] = {}
    if keys:
        existing_rows = list(
            db.scalars(
                select(EstimationAccuracyIssueMetric).where(
                    EstimationAccuracyIssueMetric.external_key.in_(sorted(keys))
                )
            ).all()
        )
        by_key = {row.external_key: row for row in existing_rows}

    stored: list[EstimationAccuracyIssueMetric] = []
    seen: set[str] = set()

    for d in detections:
        seen.add(d.external_key)
        row = by_key.get(d.external_key)
        if row is None:
            row = EstimationAccuracyIssueMetric(
                org_id=org_id,
                external_key=d.external_key,
                team_key=_dim_key(d.team_key),
                story_points=d.story_points,
                original_estimate_seconds=d.original_estimate_seconds,
                cycle_time_seconds=d.cycle_time_seconds,
                has_usable_estimate=d.has_usable_estimate,
                estimate_kind=d.estimate_kind.value,
                estimated_seconds=d.estimated_seconds,
                accuracy_ratio=d.accuracy_ratio,
                accuracy_bucket=d.accuracy_bucket.value,
                seconds_per_point=d.seconds_per_point,
                observed_at=d.observed_at,
                evidence_ref=d.evidence_ref,
            )
            db.add(row)
            by_key[d.external_key] = row
        else:
            row.deleted_at = None
            row.team_key = _dim_key(d.team_key)
            row.story_points = d.story_points
            row.original_estimate_seconds = d.original_estimate_seconds
            row.cycle_time_seconds = d.cycle_time_seconds
            row.has_usable_estimate = d.has_usable_estimate
            row.estimate_kind = d.estimate_kind.value
            row.estimated_seconds = d.estimated_seconds
            row.accuracy_ratio = d.accuracy_ratio
            row.accuracy_bucket = d.accuracy_bucket.value
            row.seconds_per_point = d.seconds_per_point
            row.observed_at = d.observed_at
            row.evidence_ref = d.evidence_ref
        stored.append(row)

    if replace_keys is not None:
        now = datetime.now(UTC)
        for key in replace_keys:
            if key in seen:
                continue
            row = by_key.get(key)
            if row is not None and row.deleted_at is None:
                row.deleted_at = now

    db.flush()
    return stored


def rebuild_estimation_accuracy_aggregates(db: Session) -> int:
    """Replace active team x month aggregates from active per-issue metrics."""
    org_id = require_current_org_id()
    metrics = list(
        db.scalars(
            select(EstimationAccuracyIssueMetric).where(
                EstimationAccuracyIssueMetric.deleted_at.is_(None)
            )
        ).all()
    )
    detections = [
        EstimationAccuracyIssueDetection(
            external_key=row.external_key,
            team_key=row.team_key or None,
            story_points=row.story_points,
            original_estimate_seconds=row.original_estimate_seconds,
            cycle_time_seconds=row.cycle_time_seconds,
            has_usable_estimate=row.has_usable_estimate,
            estimate_kind=EstimateKind(row.estimate_kind),
            estimated_seconds=row.estimated_seconds,
            accuracy_ratio=row.accuracy_ratio,
            accuracy_bucket=AccuracyBucket(row.accuracy_bucket),
            seconds_per_point=row.seconds_per_point,
            observed_at=row.observed_at,
        )
        for row in metrics
    ]
    desired = aggregate_estimation_accuracy(detections)

    existing = list(db.scalars(select(EstimationAccuracyAggregate)).all())
    by_identity = {
        (row.period_start, row.dimension, row.dimension_key): row for row in existing
    }

    for agg in desired:
        identity = (agg.period_start, agg.dimension.value, agg.dimension_key)
        row = by_identity.pop(identity, None)
        if row is None:
            db.add(
                EstimationAccuracyAggregate(
                    org_id=org_id,
                    period_start=agg.period_start,
                    period_grain="month",
                    dimension=agg.dimension.value,
                    dimension_key=agg.dimension_key,
                    issue_count=agg.issue_count,
                    with_estimate_count=agg.with_estimate_count,
                    comparable_count=agg.comparable_count,
                    coverage=agg.coverage,
                    over_count=agg.over_count,
                    under_count=agg.under_count,
                    accurate_count=agg.accurate_count,
                    accuracy_ratio_sum=agg.accuracy_ratio_sum,
                    evidence_issue_keys=list(agg.evidence_issue_keys),
                )
            )
        else:
            row.deleted_at = None
            row.period_grain = "month"
            row.issue_count = agg.issue_count
            row.with_estimate_count = agg.with_estimate_count
            row.comparable_count = agg.comparable_count
            row.coverage = agg.coverage
            row.over_count = agg.over_count
            row.under_count = agg.under_count
            row.accurate_count = agg.accurate_count
            row.accuracy_ratio_sum = agg.accuracy_ratio_sum
            row.evidence_issue_keys = list(agg.evidence_issue_keys)

    now = datetime.now(UTC)
    for row in by_identity.values():
        if row.deleted_at is None:
            row.deleted_at = now

    db.flush()
    return len(desired)


def compute_and_store_estimation_accuracy_for_org(
    db: Session,
    org_id: uuid.UUID,
) -> tuple[int, int]:
    """Compute, store, and aggregate estimation accuracy for the current org.

    Caller must already be in ``use_org(org_id)``. Returns
    ``(metric_count, aggregate_count)``. Missing estimates reduce coverage only.
    """
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")

    issues_rows = list(db.scalars(select(Issue).where(Issue.deleted_at.is_(None))).all())
    outcomes = list(db.scalars(select(Outcome).where(Outcome.deleted_at.is_(None))).all())
    cycle_times: dict[str, int | None] = {
        row.external_key: row.cycle_time_seconds for row in outcomes
    }
    observed_at: dict[str, datetime] = {}
    for row in outcomes:
        when = row.first_done_at or row.last_done_at
        if when is not None:
            observed_at[row.external_key] = when

    issues = [
        CanonicalIssue(
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
            original_estimate_seconds=row.original_estimate_seconds,
        )
        for row in issues_rows
    ]

    detections = detect_estimation_accuracy(
        issues,
        cycle_times,
        observed_at_by_key=observed_at,
    )

    prior_keys = list(
        db.scalars(
            skip_tenant_enforcement(
                select(EstimationAccuracyIssueMetric.external_key).where(
                    EstimationAccuracyIssueMetric.org_id == org_id,
                    EstimationAccuracyIssueMetric.deleted_at.is_(None),
                )
            )
        ).all()
    )

    store_estimation_accuracy_metrics(
        db,
        detections,
        replace_keys=sorted({*prior_keys, *(d.external_key for d in detections)}),
    )
    aggregate_count = rebuild_estimation_accuracy_aggregates(db)
    return len(detections), aggregate_count


def _dim_key(value: str | None) -> str:
    if value is None:
        return UNASSIGNED_DIMENSION_KEY
    stripped = value.strip()
    return stripped or UNASSIGNED_DIMENSION_KEY


def _aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)
