"""Spec quality indicators — underspecification proxies, not scores (issue #20).

Per-issue indicators:

* acceptance-criteria coverage (missing/empty vs present, or mapping unavailable)
* description length (char count + empty/short/long bucket)
* clarification-like comment traffic — blocked until comment import (#69)

Aggregates by **project** and **team** only (never per-person). Naming avoids
graded "quality score" fields. Missing AC field mapping yields an explicit
``mapping_unavailable`` coverage indicator rather than a silent zero.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.analytics.reopen import month_period_start
from throughline.db.models import (
    Issue,
    JiraFieldConcept,
    JiraFieldMapping,
    SpecQualityAggregate,
    SpecQualityIssueIndicator,
)
from throughline.domain.issues import CanonicalIssue
from throughline.tenancy import require_current_org_id, skip_tenant_enforcement

UNASSIGNED_DIMENSION_KEY = ""

# Crude length buckets for description indicators (not grades).
SHORT_DESCRIPTION_MAX_CHARS = 80

# Comment traffic depends on Jira comment import (GitHub issue #69).
COMMENT_TRAFFIC_STATUS_UNAVAILABLE = "unavailable_pending_issue_69"
COMMENT_IMPORT_DEPENDENCY_ISSUE = 69


class AcCoverage(StrEnum):
    """AC field coverage indicator — not a quality grade."""

    MAPPING_UNAVAILABLE = "mapping_unavailable"
    MISSING_OR_EMPTY = "missing_or_empty"
    PRESENT = "present"


class DescriptionLengthBucket(StrEnum):
    """Description length bucket indicator — not a quality grade."""

    EMPTY = "empty"
    SHORT = "short"
    LONG = "long"


class SpecQualityDimension(StrEnum):
    """Aggregation slices for spec-quality indicators (no person scoreboards)."""

    PROJECT = "project"
    TEAM = "team"


@dataclass(frozen=True, slots=True)
class SpecQualityIssueDetection:
    """One issue's underspecification indicators with drill-down evidence."""

    external_key: str
    project_key: str | None
    team_key: str | None
    ac_coverage: AcCoverage
    description_length_chars: int
    description_length_bucket: DescriptionLengthBucket
    comment_traffic_status: str
    comment_traffic_count: int | None
    observed_at: datetime

    @property
    def evidence_ref(self) -> str:
        return f"issue:{self.external_key}/spec-quality-indicators"


@dataclass(frozen=True, slots=True)
class SpecQualityAggregateRow:
    """One project/team x month rollup of indicator counts (not a score)."""

    period_start: date
    dimension: SpecQualityDimension
    dimension_key: str
    issue_count: int
    ac_present_count: int
    ac_missing_or_empty_count: int
    ac_mapping_unavailable_count: int
    description_empty_count: int
    description_short_count: int
    description_long_count: int
    description_length_chars_sum: int
    comment_traffic_unavailable: bool
    evidence_issue_keys: tuple[str, ...]


def is_blank_text(value: str | None) -> bool:
    """True when text is missing or whitespace-only."""
    return value is None or not value.strip()


def description_length_chars(description: str | None) -> int:
    """Character length of description text (0 when blank)."""
    if description is None:
        return 0
    return len(description)


def description_length_bucket(
    length: int,
    *,
    short_max: int = SHORT_DESCRIPTION_MAX_CHARS,
) -> DescriptionLengthBucket:
    """Map a description length to an empty/short/long indicator bucket."""
    if length <= 0:
        return DescriptionLengthBucket.EMPTY
    if length <= short_max:
        return DescriptionLengthBucket.SHORT
    return DescriptionLengthBucket.LONG


def ac_coverage_for_issue(
    acceptance_criteria: str | None,
    *,
    ac_field_mapped: bool,
) -> AcCoverage:
    """Resolve AC coverage indicator; never silent-zero when mapping is missing."""
    if not ac_field_mapped:
        return AcCoverage.MAPPING_UNAVAILABLE
    if is_blank_text(acceptance_criteria):
        return AcCoverage.MISSING_OR_EMPTY
    return AcCoverage.PRESENT


def comment_traffic_indicator(
    *,
    comments_available: bool = False,
    clarification_like_count: int | None = None,
) -> tuple[str, int | None]:
    """Comment-traffic indicator.

    Until issue comment import (#69) lands, always returns
    ``(unavailable_pending_issue_69, None)``. When comments become available,
    ``clarification_like_count`` of 0 means no traffic; >0 means traffic present.
    """
    if not comments_available:
        return COMMENT_TRAFFIC_STATUS_UNAVAILABLE, None
    count = 0 if clarification_like_count is None else max(0, int(clarification_like_count))
    if count == 0:
        return "none", 0
    return "present", count


def detect_spec_quality_for_issue(
    issue: CanonicalIssue,
    *,
    ac_field_mapped: bool,
    comments_available: bool = False,
    clarification_like_count: int | None = None,
    observed_at: datetime | None = None,
    short_max: int = SHORT_DESCRIPTION_MAX_CHARS,
) -> SpecQualityIssueDetection:
    """Compute underspecification indicators for one canonical issue."""
    length = description_length_chars(issue.description)
    traffic_status, traffic_count = comment_traffic_indicator(
        comments_available=comments_available,
        clarification_like_count=clarification_like_count,
    )
    when = observed_at
    if when is None:
        when = issue.updated_at or issue.created_at or datetime.now(UTC)
    return SpecQualityIssueDetection(
        external_key=issue.external_key,
        project_key=issue.project_key,
        team_key=issue.team_key,
        ac_coverage=ac_coverage_for_issue(
            issue.acceptance_criteria,
            ac_field_mapped=ac_field_mapped,
        ),
        description_length_chars=length,
        description_length_bucket=description_length_bucket(length, short_max=short_max),
        comment_traffic_status=traffic_status,
        comment_traffic_count=traffic_count,
        observed_at=_aware(when),
    )


def aggregate_spec_quality(
    detections: Sequence[SpecQualityIssueDetection],
) -> list[SpecQualityAggregateRow]:
    """Roll up indicators by calendar month x project and team."""

    @dataclass
    class _Bucket:
        issue_count: int = 0
        ac_present_count: int = 0
        ac_missing_or_empty_count: int = 0
        ac_mapping_unavailable_count: int = 0
        description_empty_count: int = 0
        description_short_count: int = 0
        description_long_count: int = 0
        description_length_chars_sum: int = 0
        comment_traffic_unavailable: bool = False
        evidence_issue_keys: list[str] | None = None

        def __post_init__(self) -> None:
            if self.evidence_issue_keys is None:
                self.evidence_issue_keys = []

    buckets: dict[tuple[date, SpecQualityDimension, str], _Bucket] = defaultdict(_Bucket)

    for d in detections:
        period = month_period_start(d.observed_at)
        slices = (
            (SpecQualityDimension.PROJECT, _dim_key(d.project_key)),
            (SpecQualityDimension.TEAM, _dim_key(d.team_key)),
        )
        for dimension, key in slices:
            b = buckets[(period, dimension, key)]
            b.issue_count += 1
            if d.ac_coverage == AcCoverage.PRESENT:
                b.ac_present_count += 1
            elif d.ac_coverage == AcCoverage.MISSING_OR_EMPTY:
                b.ac_missing_or_empty_count += 1
            else:
                b.ac_mapping_unavailable_count += 1
            if d.description_length_bucket == DescriptionLengthBucket.EMPTY:
                b.description_empty_count += 1
            elif d.description_length_bucket == DescriptionLengthBucket.SHORT:
                b.description_short_count += 1
            else:
                b.description_long_count += 1
            b.description_length_chars_sum += d.description_length_chars
            if d.comment_traffic_status == COMMENT_TRAFFIC_STATUS_UNAVAILABLE:
                b.comment_traffic_unavailable = True
            assert b.evidence_issue_keys is not None
            b.evidence_issue_keys.append(d.external_key)

    rows: list[SpecQualityAggregateRow] = []
    for (period, dimension, key), b in sorted(
        buckets.items(),
        key=lambda item: (item[0][0].isoformat(), item[0][1].value, item[0][2]),
    ):
        assert b.evidence_issue_keys is not None
        rows.append(
            SpecQualityAggregateRow(
                period_start=period,
                dimension=dimension,
                dimension_key=key,
                issue_count=b.issue_count,
                ac_present_count=b.ac_present_count,
                ac_missing_or_empty_count=b.ac_missing_or_empty_count,
                ac_mapping_unavailable_count=b.ac_mapping_unavailable_count,
                description_empty_count=b.description_empty_count,
                description_short_count=b.description_short_count,
                description_long_count=b.description_long_count,
                description_length_chars_sum=b.description_length_chars_sum,
                comment_traffic_unavailable=b.comment_traffic_unavailable,
                evidence_issue_keys=tuple(sorted(set(b.evidence_issue_keys))),
            )
        )
    return rows


def store_spec_quality_indicators(
    db: Session,
    detections: Sequence[SpecQualityIssueDetection],
    *,
    replace_keys: Sequence[str] | None = None,
) -> list[SpecQualityIssueIndicator]:
    """Upsert per-issue indicators. Idempotent on ``(org_id, external_key)``."""
    org_id = require_current_org_id()
    keys = {d.external_key for d in detections}
    if replace_keys is not None:
        keys.update(replace_keys)

    by_key: dict[str, SpecQualityIssueIndicator] = {}
    if keys:
        existing_rows = list(
            db.scalars(
                select(SpecQualityIssueIndicator).where(
                    SpecQualityIssueIndicator.external_key.in_(sorted(keys))
                )
            ).all()
        )
        by_key = {row.external_key: row for row in existing_rows}
    stored: list[SpecQualityIssueIndicator] = []
    seen: set[str] = set()

    for d in detections:
        seen.add(d.external_key)
        row = by_key.get(d.external_key)
        if row is None:
            row = SpecQualityIssueIndicator(
                org_id=org_id,
                external_key=d.external_key,
                project_key=_dim_key(d.project_key),
                team_key=_dim_key(d.team_key),
                ac_coverage=d.ac_coverage.value,
                description_length_chars=d.description_length_chars,
                description_length_bucket=d.description_length_bucket.value,
                comment_traffic_status=d.comment_traffic_status,
                comment_traffic_count=d.comment_traffic_count,
                observed_at=d.observed_at,
                evidence_ref=d.evidence_ref,
            )
            db.add(row)
            by_key[d.external_key] = row
        else:
            row.deleted_at = None
            row.project_key = _dim_key(d.project_key)
            row.team_key = _dim_key(d.team_key)
            row.ac_coverage = d.ac_coverage.value
            row.description_length_chars = d.description_length_chars
            row.description_length_bucket = d.description_length_bucket.value
            row.comment_traffic_status = d.comment_traffic_status
            row.comment_traffic_count = d.comment_traffic_count
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


def rebuild_spec_quality_aggregates(db: Session) -> int:
    """Replace active aggregates from active per-issue indicators for this org."""
    org_id = require_current_org_id()
    indicators = list(
        db.scalars(
            select(SpecQualityIssueIndicator).where(
                SpecQualityIssueIndicator.deleted_at.is_(None)
            )
        ).all()
    )
    detections = [
        SpecQualityIssueDetection(
            external_key=row.external_key,
            project_key=row.project_key or None,
            team_key=row.team_key or None,
            ac_coverage=AcCoverage(row.ac_coverage),
            description_length_chars=row.description_length_chars,
            description_length_bucket=DescriptionLengthBucket(row.description_length_bucket),
            comment_traffic_status=row.comment_traffic_status,
            comment_traffic_count=row.comment_traffic_count,
            observed_at=row.observed_at,
        )
        for row in indicators
    ]
    desired = aggregate_spec_quality(detections)

    existing = list(db.scalars(select(SpecQualityAggregate)).all())
    by_identity = {
        (row.period_start, row.dimension, row.dimension_key): row for row in existing
    }

    for agg in desired:
        identity = (agg.period_start, agg.dimension.value, agg.dimension_key)
        row = by_identity.pop(identity, None)
        if row is None:
            db.add(
                SpecQualityAggregate(
                    org_id=org_id,
                    period_start=agg.period_start,
                    period_grain="month",
                    dimension=agg.dimension.value,
                    dimension_key=agg.dimension_key,
                    issue_count=agg.issue_count,
                    ac_present_count=agg.ac_present_count,
                    ac_missing_or_empty_count=agg.ac_missing_or_empty_count,
                    ac_mapping_unavailable_count=agg.ac_mapping_unavailable_count,
                    description_empty_count=agg.description_empty_count,
                    description_short_count=agg.description_short_count,
                    description_long_count=agg.description_long_count,
                    description_length_chars_sum=agg.description_length_chars_sum,
                    comment_traffic_unavailable=agg.comment_traffic_unavailable,
                    evidence_issue_keys=list(agg.evidence_issue_keys),
                )
            )
        else:
            row.deleted_at = None
            row.period_grain = "month"
            row.issue_count = agg.issue_count
            row.ac_present_count = agg.ac_present_count
            row.ac_missing_or_empty_count = agg.ac_missing_or_empty_count
            row.ac_mapping_unavailable_count = agg.ac_mapping_unavailable_count
            row.description_empty_count = agg.description_empty_count
            row.description_short_count = agg.description_short_count
            row.description_long_count = agg.description_long_count
            row.description_length_chars_sum = agg.description_length_chars_sum
            row.comment_traffic_unavailable = agg.comment_traffic_unavailable
            row.evidence_issue_keys = list(agg.evidence_issue_keys)

    now = datetime.now(UTC)
    for row in by_identity.values():
        if row.deleted_at is None:
            row.deleted_at = now

    db.flush()
    return len(desired)


def org_has_ac_field_mapping(db: Session) -> bool:
    """True when the current org has a non-deleted acceptance-criteria mapping."""
    row = db.scalar(
        select(JiraFieldMapping).where(
            JiraFieldMapping.concept == JiraFieldConcept.ACCEPTANCE_CRITERIA,
            JiraFieldMapping.deleted_at.is_(None),
        )
    )
    if row is None:
        return False
    return bool(row.jira_field_id and str(row.jira_field_id).strip())


def compute_and_store_spec_quality_for_org(
    db: Session,
    org_id: uuid.UUID,
    *,
    comments_available: bool = False,
    short_max: int = SHORT_DESCRIPTION_MAX_CHARS,
) -> tuple[int, int]:
    """Compute, store, and aggregate spec-quality indicators for the current org.

    Caller must already be in ``use_org(org_id)``. Returns
    ``(indicator_count, aggregate_count)``.

    Comment traffic defaults to unavailable pending #69 unless
    ``comments_available`` is explicitly True (tests / future #69 wiring).
    """
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")

    ac_mapped = org_has_ac_field_mapping(db)
    issues = list(db.scalars(select(Issue).where(Issue.deleted_at.is_(None))).all())

    detections: list[SpecQualityIssueDetection] = []
    for row in issues:
        issue = CanonicalIssue(
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
        detections.append(
            detect_spec_quality_for_issue(
                issue,
                ac_field_mapped=ac_mapped,
                comments_available=comments_available,
                short_max=short_max,
            )
        )

    # Also soft-delete indicators for issues that disappeared.
    prior_keys = list(
        db.scalars(
            skip_tenant_enforcement(
                select(SpecQualityIssueIndicator.external_key).where(
                    SpecQualityIssueIndicator.org_id == org_id,
                    SpecQualityIssueIndicator.deleted_at.is_(None),
                )
            )
        ).all()
    )

    store_spec_quality_indicators(
        db,
        detections,
        replace_keys=sorted({*prior_keys, *(d.external_key for d in detections)}),
    )
    aggregate_count = rebuild_spec_quality_aggregates(db)
    return len(detections), aggregate_count


def _dim_key(value: str | None) -> str:
    if value is None:
        return UNASSIGNED_DIMENSION_KEY
    stripped = value.strip()
    return stripped or UNASSIGNED_DIMENSION_KEY


def _aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when
