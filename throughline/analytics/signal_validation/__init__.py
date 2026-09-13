"""Signal validation study runner (issue #26).

Loads ≥2 ToS-verified public Jira corpora, runs the full diagnostic report
pipeline on each, and writes a machine-readable comparison artifact so the
study can be re-run. Findings narrative lives in ``_docs/signal-validation-study.md``.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from throughline.analytics.diagnostic_report import (
    EXPECTED_METRIC_KEYS,
    generate_diagnostic_report,
)
from throughline.db.models import (
    DiagnosticReport,
    EstimationAccuracyAggregate,
    EstimationAccuracyIssueMetric,
    Issue,
    IssueFieldChange,
    IssueTransition,
    JiraIssue,
    JiraStatusTransition,
    LateChildEvent,
    Outcome,
    ReopenAggregate,
    ReopenEvent,
    SpecChangeEvent,
    SpecQualityAggregate,
    SpecQualityIssueIndicator,
    SyncState,
)
from throughline.ingest.corpus.loader import (
    SOURCE_CONFIGS,
    ensure_corpus_org,
    load_corpus,
)
from throughline.ingest.corpus.tos import (
    JENKINS_SOURCE_ID,
    SOURCE_ID,
    ensure_tos_allowed,
    load_tos_statuses,
    repo_root as corpus_repo_root,
)
from throughline.ingest.normalize import (
    normalize_issues_for_org,
    normalize_transitions_for_org,
)
from throughline.tenancy import use_org

_RESET_MODELS = (
    EstimationAccuracyAggregate,
    EstimationAccuracyIssueMetric,
    SpecQualityAggregate,
    SpecQualityIssueIndicator,
    SpecChangeEvent,
    LateChildEvent,
    ReopenAggregate,
    ReopenEvent,
    Outcome,
    IssueTransition,
    IssueFieldChange,
    Issue,
    JiraStatusTransition,
    JiraIssue,
    SyncState,
    DiagnosticReport,
)


def _reset_org_study_data(db: Session, org_id) -> None:
    """Drop prior import/analytics rows so fixture and remote runs do not mix."""
    for model in _RESET_MODELS:
        # Must scope by org_id — Core delete() is not tenant-filtered.
        db.execute(delete(model).where(model.org_id == org_id))
    db.commit()
    db.expire_all()

DEFAULT_STUDY_SOURCES: tuple[str, ...] = (SOURCE_ID, JENKINS_SOURCE_ID)
DEFAULT_RANGE_START = date(2000, 1, 1)
DEFAULT_RANGE_END = date(2030, 12, 31)
ARTIFACT_RELATIVE = Path("_docs") / "artifacts" / "signal-validation-metrics.json"

__all__ = [
    "ARTIFACT_RELATIVE",
    "DEFAULT_RANGE_END",
    "DEFAULT_RANGE_START",
    "DEFAULT_STUDY_SOURCES",
    "SignalValidationStudy",
    "SourceStudyResult",
    "parse_iso_date",
    "run_signal_validation_study",
    "run_source_study",
    "study_to_dict",
    "write_study_artifact",
]


@dataclass(frozen=True, slots=True)
class SourceStudyResult:
    """One corpus load + diagnostic report snapshot."""

    source: str
    mode: str
    org_id: str
    org_name: str
    issue_count: int
    transition_count: int
    tos_verified_date: str
    tos_status: str
    report_id: str
    report_version: int
    report_status: str
    range_start: str
    range_end: str
    metrics: dict[str, Any]
    generation_detail: dict[str, Any]
    error_message: str | None


@dataclass(frozen=True, slots=True)
class SignalValidationStudy:
    """Comparison across sources for the Phase 0 go/no-go checkpoint."""

    sources: tuple[SourceStudyResult, ...]
    metric_keys: tuple[str, ...]
    metric_deltas: dict[str, dict[str, Any]]
    notes: tuple[str, ...]


def _metric_value(metrics: dict[str, Any], key: str) -> float | int | None:
    entry = metrics.get(key)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    if isinstance(value, (int, float)) or value is None:
        return value
    return None


def _delta_map(results: tuple[SourceStudyResult, ...]) -> dict[str, dict[str, Any]]:
    """Build per-metric value map + pairwise absolute deltas when two sources."""
    out: dict[str, dict[str, Any]] = {}
    for key in sorted(EXPECTED_METRIC_KEYS):
        by_source = {r.source: _metric_value(r.metrics, key) for r in results}
        entry: dict[str, Any] = {"by_source": by_source}
        if len(results) >= 2:
            a = by_source.get(results[0].source)
            b = by_source.get(results[1].source)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                entry["abs_delta"] = abs(a - b)
                entry["varies"] = a != b
            else:
                entry["abs_delta"] = None
                entry["varies"] = None
        out[key] = entry
    return out


def run_source_study(
    db: Session,
    *,
    source: str,
    mode: str = "fixture",
    range_start: date = DEFAULT_RANGE_START,
    range_end: date = DEFAULT_RANGE_END,
    jql: str | None = None,
    page_size: int = 25,
    max_pages: int | None = 2,
    max_issues: int | None = 40,
    min_delay_seconds: float | None = None,
    root: Path | None = None,
) -> SourceStudyResult:
    """Load one corpus source and generate a diagnostic report for it."""
    if source not in SOURCE_CONFIGS:
        raise ValueError(f"Unknown source={source!r}; expected {sorted(SOURCE_CONFIGS)}")
    tos = ensure_tos_allowed(root=root, source=source)
    org = ensure_corpus_org(db, source=source)
    _reset_org_study_data(db, org.id)
    db.expire_all()
    org = ensure_corpus_org(db, source=source)
    load = load_corpus(
        db,
        mode=mode,
        source=source,
        jql=jql,
        page_size=page_size,
        max_pages=max_pages,
        max_issues=max_issues,
        min_delay_seconds=min_delay_seconds,
        root=root,
    )
    cfg = SOURCE_CONFIGS[source]
    with use_org(load.org_id):
        # Defensive: ensure analytics tables are populated even if a prior
        # changelog pass marked issues complete without canonical sync.
        normalize_issues_for_org(db, load.org_id)
        normalize_transitions_for_org(db, load.org_id)
        db.commit()
        report = generate_diagnostic_report(db, load.org_id, range_start, range_end)
        db.commit()
        return SourceStudyResult(
            source=source,
            mode=load.mode,
            org_id=str(load.org_id),
            org_name=cfg.org_name,
            issue_count=load.issue_count,
            transition_count=load.transition_count,
            tos_verified_date=tos.verified_date,
            tos_status=tos.status,
            report_id=str(report.id),
            report_version=report.version,
            report_status=report.status,
            range_start=range_start.isoformat(),
            range_end=range_end.isoformat(),
            metrics=dict(report.metrics or {}),
            generation_detail=dict(report.generation_detail or {}),
            error_message=report.error_message,
        )


def run_signal_validation_study(
    db: Session,
    *,
    sources: tuple[str, ...] | None = None,
    mode: str = "fixture",
    range_start: date = DEFAULT_RANGE_START,
    range_end: date = DEFAULT_RANGE_END,
    jql_by_source: dict[str, str] | None = None,
    page_size: int = 25,
    max_pages: int | None = 2,
    max_issues: int | None = 40,
    min_delay_seconds: float | None = None,
    root: Path | None = None,
) -> SignalValidationStudy:
    """Run the study across ``sources`` (default: ASF + Jenkins)."""
    selected = sources or DEFAULT_STUDY_SOURCES
    if len(selected) < 2:
        raise ValueError("signal validation requires at least two sources")
    statuses = load_tos_statuses(root=root)
    for src in selected:
        if src not in statuses or statuses[src].status != "allowed":
            ensure_tos_allowed(root=root, source=src)

    jql_map = jql_by_source or {}
    results: list[SourceStudyResult] = []
    for src in selected:
        results.append(
            run_source_study(
                db,
                source=src,
                mode=mode,
                range_start=range_start,
                range_end=range_end,
                jql=jql_map.get(src),
                page_size=page_size,
                max_pages=max_pages,
                max_issues=max_issues,
                min_delay_seconds=min_delay_seconds,
                root=root,
            )
        )

    frozen = tuple(results)
    return SignalValidationStudy(
        sources=frozen,
        metric_keys=tuple(sorted(EXPECTED_METRIC_KEYS)),
        metric_deltas=_delta_map(frozen),
        notes=_build_notes(frozen),
    )


def _build_notes(results: tuple[SourceStudyResult, ...]) -> tuple[str, ...]:
    notes: list[str] = []
    for r in results:
        notes.append(
            f"{r.source}: mode={r.mode} issues={r.issue_count} "
            f"transitions={r.transition_count} report={r.report_status} "
            f"v{r.report_version}"
        )
    reopen = [_metric_value(r.metrics, "reopen.event_count") for r in results]
    completed = [
        _metric_value(r.metrics, "cycle_time.completed_issue_count") for r in results
    ]
    ac_missing = [
        _metric_value(r.metrics, "spec_quality.ac_missing_or_empty_count")
        for r in results
    ]
    if all(v == 0 or v is None for v in reopen):
        notes.append(
            "reopen.event_count is zero/null on all sources in this run — "
            "churn signal may be absent or out of date range / sample."
        )
    elif any(isinstance(v, (int, float)) and v > 0 for v in reopen):
        notes.append(
            "reopen events present on at least one source — compare rates "
            f"across sources: {dict(zip([r.source for r in results], reopen, strict=True))}"
        )
    if all(v == 0 or v is None for v in completed):
        notes.append(
            "cycle_time.completed_issue_count is zero/null on all sources — "
            "status names may not map to in_progress→done lanes, or sample "
            "lacks completed passes."
        )
    if any(v is None for v in ac_missing) or all((v or 0) == 0 for v in ac_missing):
        notes.append(
            "spec_quality AC-missing counts are zero/null — public corpora "
            "often lack AC field mappings; reopen↔spec-quality correlation "
            "cannot be validated without mapped AC fields."
        )
    # Directional check: do sources with more reopens also show more AC gaps?
    paired = [
        (r, _metric_value(r.metrics, "reopen.event_count"), _metric_value(r.metrics, "spec_quality.ac_missing_or_empty_count"))
        for r in results
    ]
    if (
        len(paired) >= 2
        and all(isinstance(a, (int, float)) and isinstance(b, (int, float)) for _, a, b in paired)
    ):
        reopen_vals = [float(a) for _, a, _ in paired]  # type: ignore[arg-type]
        ac_vals = [float(b) for _, _, b in paired]  # type: ignore[arg-type]
        if max(reopen_vals) != min(reopen_vals) and max(ac_vals) != min(ac_vals):
            same_direction = (reopen_vals[0] - reopen_vals[1]) * (ac_vals[0] - ac_vals[1]) > 0
            notes.append(
                "reopen_vs_ac_missing_cross_source_same_direction="
                f"{same_direction} (n={len(paired)}; not statistically powered)"
            )
        else:
            notes.append(
                "reopen_vs_ac_missing_cross_source: insufficient variance to "
                "judge co-movement"
            )
    varying = [
        key
        for key, entry in _delta_map(results).items()
        if entry.get("varies") is True
    ]
    notes.append(
        f"metrics_with_nonzero_cross_source_delta="
        f"{len(varying)}/{len(EXPECTED_METRIC_KEYS)}"
    )
    return tuple(notes)


def study_to_dict(study: SignalValidationStudy) -> dict[str, Any]:
    return {
        "sources": [asdict(s) for s in study.sources],
        "metric_keys": list(study.metric_keys),
        "metric_deltas": study.metric_deltas,
        "notes": list(study.notes),
    }


def write_study_artifact(
    study: SignalValidationStudy,
    *,
    path: Path | None = None,
    root: Path | None = None,
) -> Path:
    """Persist the comparison JSON under ``_docs/artifacts/``."""
    out = path or ((root or corpus_repo_root()) / ARTIFACT_RELATIVE)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(study_to_dict(study), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def parse_iso_date(value: str) -> date:
    return date.fromisoformat(value)
