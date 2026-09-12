"""Versioned diagnostic report aggregation (issue #22).

Runs available Phase 0 analytics (#17-#21), snapshots metric values plus
evidence refs for an org date range into an immutable versioned row, and
surfaces ``success`` / ``partial`` / ``failed`` status visibly.
"""

from __future__ import annotations

import statistics
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from throughline.analytics.cycle_time import compute_and_store_outcomes_for_org
from throughline.analytics.estimation_accuracy import (
    compute_and_store_estimation_accuracy_for_org,
)
from throughline.analytics.reopen import compute_and_store_reopens_for_org
from throughline.analytics.scope_change import compute_and_store_scope_change_for_org
from throughline.analytics.spec_quality import compute_and_store_spec_quality_for_org
from throughline.db.models import (
    DiagnosticReport,
    DiagnosticReportStatus,
    EstimationAccuracyIssueMetric,
    LateChildEvent,
    Outcome,
    ReopenEvent,
    SpecChangeEvent,
    SpecQualityIssueIndicator,
)
from throughline.tenancy import require_current_org_id, skip_tenant_enforcement

# Stable metric keys asserted by tests and consumed by the report API (#23).
METRIC_CYCLE_TIME_COMPLETED_COUNT = "cycle_time.completed_issue_count"
METRIC_CYCLE_TIME_MEDIAN_SECONDS = "cycle_time.median_seconds"
METRIC_REOPEN_EVENT_COUNT = "reopen.event_count"
METRIC_SCOPE_LATE_CHILD_COUNT = "scope_change.late_child_count"
METRIC_SCOPE_SPEC_CHANGE_COUNT = "scope_change.spec_change_count"
METRIC_SPEC_QUALITY_ISSUE_COUNT = "spec_quality.issue_count"
METRIC_SPEC_QUALITY_AC_MISSING_COUNT = "spec_quality.ac_missing_or_empty_count"
METRIC_ESTIMATION_ISSUE_COUNT = "estimation_accuracy.issue_count"
METRIC_ESTIMATION_COVERAGE = "estimation_accuracy.coverage"
METRIC_ESTIMATION_OVER_COUNT = "estimation_accuracy.over_count"
METRIC_ESTIMATION_UNDER_COUNT = "estimation_accuracy.under_count"
METRIC_ESTIMATION_ACCURATE_COUNT = "estimation_accuracy.accurate_count"

EXPECTED_METRIC_KEYS: frozenset[str] = frozenset(
    {
        METRIC_CYCLE_TIME_COMPLETED_COUNT,
        METRIC_CYCLE_TIME_MEDIAN_SECONDS,
        METRIC_REOPEN_EVENT_COUNT,
        METRIC_SCOPE_LATE_CHILD_COUNT,
        METRIC_SCOPE_SPEC_CHANGE_COUNT,
        METRIC_SPEC_QUALITY_ISSUE_COUNT,
        METRIC_SPEC_QUALITY_AC_MISSING_COUNT,
        METRIC_ESTIMATION_ISSUE_COUNT,
        METRIC_ESTIMATION_COVERAGE,
        METRIC_ESTIMATION_OVER_COUNT,
        METRIC_ESTIMATION_UNDER_COUNT,
        METRIC_ESTIMATION_ACCURATE_COUNT,
    }
)


class MetricFamily(StrEnum):
    """Analytics families included in a diagnostic report."""

    CYCLE_TIME = "cycle_time"
    REOPEN = "reopen"
    SCOPE_CHANGE = "scope_change"
    SPEC_QUALITY = "spec_quality"
    ESTIMATION_ACCURACY = "estimation_accuracy"


ALL_METRIC_FAMILIES: tuple[MetricFamily, ...] = tuple(MetricFamily)


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """One report metric value with drill-down evidence refs."""

    key: str
    value: float | int | None
    evidence_refs: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "evidence_refs": list(self.evidence_refs),
        }


def _metric_entry(
    key: str,
    value: float | int | None,
    evidence_refs: Sequence[str],
) -> MetricSnapshot:
    return MetricSnapshot(key=key, value=value, evidence_refs=tuple(evidence_refs))


def _in_range(when: datetime | None, range_start: date, range_end: date) -> bool:
    if when is None:
        return False
    day = when.astimezone(UTC).date() if when.tzinfo else when.date()
    return range_start <= day <= range_end


def _cycle_time_evidence_ref(external_key: str) -> str:
    return f"issue:{external_key}/cycle-time"


def snapshot_cycle_time_metrics(
    db: Session,
    *,
    range_start: date,
    range_end: date,
) -> dict[str, MetricSnapshot]:
    """Snapshot completed cycle-time outcomes in the date range."""
    rows = list(db.scalars(select(Outcome).where(Outcome.deleted_at.is_(None))).all())
    in_range = [
        row
        for row in rows
        if row.cycle_time_seconds is not None
        and _in_range(row.first_done_at or row.last_done_at, range_start, range_end)
    ]
    refs = tuple(sorted(_cycle_time_evidence_ref(r.external_key) for r in in_range))
    seconds = [int(r.cycle_time_seconds) for r in in_range if r.cycle_time_seconds is not None]
    median = float(statistics.median(seconds)) if seconds else None
    return {
        METRIC_CYCLE_TIME_COMPLETED_COUNT: _metric_entry(
            METRIC_CYCLE_TIME_COMPLETED_COUNT,
            len(in_range),
            refs,
        ),
        METRIC_CYCLE_TIME_MEDIAN_SECONDS: _metric_entry(
            METRIC_CYCLE_TIME_MEDIAN_SECONDS,
            median,
            refs,
        ),
    }


def snapshot_reopen_metrics(
    db: Session,
    *,
    range_start: date,
    range_end: date,
) -> dict[str, MetricSnapshot]:
    rows = list(
        db.scalars(select(ReopenEvent).where(ReopenEvent.deleted_at.is_(None))).all()
    )
    in_range = [r for r in rows if _in_range(r.transitioned_at, range_start, range_end)]
    refs = tuple(sorted({r.evidence_ref for r in in_range}))
    return {
        METRIC_REOPEN_EVENT_COUNT: _metric_entry(
            METRIC_REOPEN_EVENT_COUNT,
            len(in_range),
            refs,
        ),
    }


def snapshot_scope_change_metrics(
    db: Session,
    *,
    range_start: date,
    range_end: date,
) -> dict[str, MetricSnapshot]:
    late = list(
        db.scalars(select(LateChildEvent).where(LateChildEvent.deleted_at.is_(None))).all()
    )
    specs = list(
        db.scalars(select(SpecChangeEvent).where(SpecChangeEvent.deleted_at.is_(None))).all()
    )
    late_in = [r for r in late if _in_range(r.child_created_at, range_start, range_end)]
    spec_in = [r for r in specs if _in_range(r.changed_at, range_start, range_end)]
    return {
        METRIC_SCOPE_LATE_CHILD_COUNT: _metric_entry(
            METRIC_SCOPE_LATE_CHILD_COUNT,
            len(late_in),
            tuple(sorted({r.evidence_ref for r in late_in})),
        ),
        METRIC_SCOPE_SPEC_CHANGE_COUNT: _metric_entry(
            METRIC_SCOPE_SPEC_CHANGE_COUNT,
            len(spec_in),
            tuple(sorted({r.evidence_ref for r in spec_in})),
        ),
    }


def snapshot_spec_quality_metrics(
    db: Session,
    *,
    range_start: date,
    range_end: date,
) -> dict[str, MetricSnapshot]:
    rows = list(
        db.scalars(
            select(SpecQualityIssueIndicator).where(
                SpecQualityIssueIndicator.deleted_at.is_(None)
            )
        ).all()
    )
    in_range = [r for r in rows if _in_range(r.observed_at, range_start, range_end)]
    refs = tuple(sorted({r.evidence_ref for r in in_range}))
    missing = sum(1 for r in in_range if r.ac_coverage == "missing_or_empty")
    return {
        METRIC_SPEC_QUALITY_ISSUE_COUNT: _metric_entry(
            METRIC_SPEC_QUALITY_ISSUE_COUNT,
            len(in_range),
            refs,
        ),
        METRIC_SPEC_QUALITY_AC_MISSING_COUNT: _metric_entry(
            METRIC_SPEC_QUALITY_AC_MISSING_COUNT,
            missing,
            refs,
        ),
    }


def snapshot_estimation_accuracy_metrics(
    db: Session,
    *,
    range_start: date,
    range_end: date,
) -> dict[str, MetricSnapshot]:
    rows = list(
        db.scalars(
            select(EstimationAccuracyIssueMetric).where(
                EstimationAccuracyIssueMetric.deleted_at.is_(None)
            )
        ).all()
    )
    in_range = [r for r in rows if _in_range(r.observed_at, range_start, range_end)]
    refs = tuple(sorted({r.evidence_ref for r in in_range}))
    with_estimate = sum(1 for r in in_range if r.has_usable_estimate)
    issue_count = len(in_range)
    coverage = (with_estimate / issue_count) if issue_count else 0.0
    return {
        METRIC_ESTIMATION_ISSUE_COUNT: _metric_entry(
            METRIC_ESTIMATION_ISSUE_COUNT,
            issue_count,
            refs,
        ),
        METRIC_ESTIMATION_COVERAGE: _metric_entry(
            METRIC_ESTIMATION_COVERAGE,
            coverage,
            refs,
        ),
        METRIC_ESTIMATION_OVER_COUNT: _metric_entry(
            METRIC_ESTIMATION_OVER_COUNT,
            sum(1 for r in in_range if r.accuracy_bucket == "over"),
            refs,
        ),
        METRIC_ESTIMATION_UNDER_COUNT: _metric_entry(
            METRIC_ESTIMATION_UNDER_COUNT,
            sum(1 for r in in_range if r.accuracy_bucket == "under"),
            refs,
        ),
        METRIC_ESTIMATION_ACCURATE_COUNT: _metric_entry(
            METRIC_ESTIMATION_ACCURATE_COUNT,
            sum(1 for r in in_range if r.accuracy_bucket == "accurate"),
            refs,
        ),
    }


_FAMILY_COMPUTE: Mapping[
    MetricFamily,
    Callable[[Session, uuid.UUID], Any],
] = {
    MetricFamily.CYCLE_TIME: compute_and_store_outcomes_for_org,
    MetricFamily.REOPEN: compute_and_store_reopens_for_org,
    MetricFamily.SCOPE_CHANGE: compute_and_store_scope_change_for_org,
    MetricFamily.SPEC_QUALITY: compute_and_store_spec_quality_for_org,
    MetricFamily.ESTIMATION_ACCURACY: compute_and_store_estimation_accuracy_for_org,
}

_FAMILY_SNAPSHOT: Mapping[
    MetricFamily,
    Callable[..., dict[str, MetricSnapshot]],
] = {
    MetricFamily.CYCLE_TIME: snapshot_cycle_time_metrics,
    MetricFamily.REOPEN: snapshot_reopen_metrics,
    MetricFamily.SCOPE_CHANGE: snapshot_scope_change_metrics,
    MetricFamily.SPEC_QUALITY: snapshot_spec_quality_metrics,
    MetricFamily.ESTIMATION_ACCURACY: snapshot_estimation_accuracy_metrics,
}


def next_report_version(
    db: Session,
    org_id: uuid.UUID,
    range_start: date,
    range_end: date,
) -> int:
    """Return the next version number for org + date range (1-based)."""
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")

    max_version = db.scalar(
        skip_tenant_enforcement(
            select(func.max(DiagnosticReport.version)).where(
                DiagnosticReport.org_id == org_id,
                DiagnosticReport.range_start == range_start,
                DiagnosticReport.range_end == range_end,
            )
        )
    )
    return int(max_version or 0) + 1


def _finalize_status(
    family_results: Mapping[str, Mapping[str, Any]],
) -> DiagnosticReportStatus:
    oks = [bool(detail.get("ok")) for detail in family_results.values()]
    if not oks:
        return DiagnosticReportStatus.FAILED
    if all(oks):
        return DiagnosticReportStatus.SUCCESS
    if any(oks):
        return DiagnosticReportStatus.PARTIAL
    return DiagnosticReportStatus.FAILED


def generate_diagnostic_report(
    db: Session,
    org_id: uuid.UUID,
    range_start: date,
    range_end: date,
    *,
    families: Sequence[MetricFamily] | None = None,
) -> DiagnosticReport:
    """Compute analytics, snapshot metrics, and persist a new report version.

    Caller must already be in ``use_org(org_id)``. Never mutates prior versions
    for the same inputs — always inserts a new ``version`` row.
    """
    current = require_current_org_id()
    if current != org_id:
        raise ValueError("org_id must match the current tenant context")
    if range_end < range_start:
        raise ValueError("range_end must be on or after range_start")

    selected = tuple(families) if families is not None else ALL_METRIC_FAMILIES
    version = next_report_version(db, org_id, range_start, range_end)
    report = DiagnosticReport(
        org_id=org_id,
        range_start=range_start,
        range_end=range_end,
        version=version,
        status=DiagnosticReportStatus.PENDING.value,
        metrics={},
        generation_detail={},
        generated_at=None,
        error_message=None,
    )
    db.add(report)
    db.flush()

    metrics: dict[str, Any] = {}
    generation_detail: dict[str, Any] = {}
    fatal_error: str | None = None

    try:
        for family in selected:
            compute = _FAMILY_COMPUTE[family]
            snapshot = _FAMILY_SNAPSHOT[family]
            try:
                compute_result = compute(db, org_id)
                family_metrics = snapshot(db, range_start=range_start, range_end=range_end)
                for key, snap in family_metrics.items():
                    metrics[key] = snap.as_dict()
                generation_detail[family.value] = {
                    "ok": True,
                    "compute_result": _jsonable(compute_result),
                    "metric_keys": sorted(family_metrics.keys()),
                }
            except Exception as exc:
                generation_detail[family.value] = {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
        generation_detail["fatal"] = {"ok": False, "error": fatal_error}

    status = _finalize_status(
        {k: v for k, v in generation_detail.items() if k != "fatal"}
    )
    if fatal_error is not None:
        status = DiagnosticReportStatus.FAILED

    report.metrics = metrics
    report.generation_detail = generation_detail
    report.status = status.value
    report.generated_at = datetime.now(UTC)
    if status == DiagnosticReportStatus.FAILED:
        failures = [
            f"{name}: {detail.get('error', 'unknown')}"
            for name, detail in generation_detail.items()
            if not detail.get("ok")
        ]
        report.error_message = fatal_error or (
            "; ".join(failures) if failures else "report generation failed"
        )
    elif status == DiagnosticReportStatus.PARTIAL:
        failures = [
            f"{name}: {detail.get('error', 'unknown')}"
            for name, detail in generation_detail.items()
            if not detail.get("ok")
        ]
        report.error_message = "; ".join(failures) if failures else "partial report"
    else:
        report.error_message = None

    db.flush()
    return report


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
