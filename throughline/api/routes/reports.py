"""Authenticated diagnostic report read API (issue #23).

List reports, return a report's metrics payload, and paginate evidence refs
for a metric key. Org context comes from membership (#8); tenant ORM filters
(#7) keep soft-deleted and cross-org rows out of results.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from throughline.api.auth import require_auth, resolve_org_from_membership
from throughline.api.deps import get_db
from throughline.api.schemas import (
    DiagnosticMetricEntry,
    DiagnosticMetricEvidencePage,
    DiagnosticReportDetail,
    DiagnosticReportListItem,
)
from throughline.db.models import DiagnosticReport
from throughline.tenancy import use_org

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(require_auth)],
)

_DEFAULT_EVIDENCE_LIMIT = 50
_MAX_EVIDENCE_LIMIT = 200


def _load_report(db: Session, report_id: uuid.UUID) -> DiagnosticReport:
    """Load an active report for the current org or raise 404."""
    report = db.scalar(select(DiagnosticReport).where(DiagnosticReport.id == report_id))
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Report not found",
        )
    return report


def _metric_entry(raw: Any) -> DiagnosticMetricEntry:
    if not isinstance(raw, dict):
        return DiagnosticMetricEntry(value=None, evidence_refs=[])
    value = raw.get("value")
    refs = raw.get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = []
    return DiagnosticMetricEntry(
        value=value if isinstance(value, (int, float)) or value is None else None,
        evidence_refs=[str(r) for r in refs],
    )


def _report_detail(report: DiagnosticReport) -> DiagnosticReportDetail:
    metrics_raw = report.metrics if isinstance(report.metrics, dict) else {}
    metrics = {key: _metric_entry(entry) for key, entry in metrics_raw.items()}
    return DiagnosticReportDetail(
        id=report.id,
        org_id=report.org_id,
        range_start=report.range_start,
        range_end=report.range_end,
        version=report.version,
        status=report.status,
        metrics=metrics,
        generation_detail=(
            report.generation_detail if isinstance(report.generation_detail, dict) else {}
        ),
        generated_at=report.generated_at,
        error_message=report.error_message,
        created_at=report.created_at,
        updated_at=report.updated_at,
    )


@router.get("", response_model=list[DiagnosticReportListItem])
def list_reports(
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> list[DiagnosticReport]:
    """List active diagnostic reports for the current org (newest first)."""
    with use_org(org_id):
        return list(
            db.scalars(
                select(DiagnosticReport).order_by(
                    DiagnosticReport.generated_at.desc().nullslast(),
                    DiagnosticReport.created_at.desc(),
                    DiagnosticReport.version.desc(),
                )
            ).all()
        )


@router.get("/{report_id}", response_model=DiagnosticReportDetail)
def get_report(
    report_id: Annotated[uuid.UUID, Path(description="Diagnostic report id")],
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
) -> DiagnosticReportDetail:
    """Return one report's metrics payload for the current org."""
    with use_org(org_id):
        report = _load_report(db, report_id)
        return _report_detail(report)


@router.get(
    "/{report_id}/metrics/{metric_key}/evidence",
    response_model=DiagnosticMetricEvidencePage,
)
def get_report_metric_evidence(
    report_id: Annotated[uuid.UUID, Path(description="Diagnostic report id")],
    metric_key: Annotated[str, Path(description="Stable metric key from the report")],
    org_id: Annotated[uuid.UUID, Depends(resolve_org_from_membership)],
    db: Annotated[Session, Depends(get_db)],
    page: Annotated[int, Query(ge=1, description="1-based page")] = 1,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_EVIDENCE_LIMIT,
            description=f"Page size (max {_MAX_EVIDENCE_LIMIT})",
        ),
    ] = _DEFAULT_EVIDENCE_LIMIT,
) -> DiagnosticMetricEvidencePage:
    """Paginate evidence refs for one metric on a report."""
    key = metric_key.strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="metric_key is required",
        )

    with use_org(org_id):
        report = _load_report(db, report_id)
        metrics_raw = report.metrics if isinstance(report.metrics, dict) else {}
        if key not in metrics_raw:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Metric '{key}' not found on report",
            )

        entry = _metric_entry(metrics_raw[key])
        total = len(entry.evidence_refs)
        offset = (page - 1) * limit
        items = entry.evidence_refs[offset : offset + limit]
        return DiagnosticMetricEvidencePage(
            report_id=report.id,
            metric_key=key,
            value=entry.value,
            items=items,
            page=page,
            limit=limit,
            total=total,
            has_more=offset + len(items) < total,
        )
