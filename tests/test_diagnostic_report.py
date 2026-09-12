"""Diagnostic report aggregation: versioned snapshot + arq job (issue #22)."""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics import diagnostic_report as diagnostic_report_mod
from throughline.analytics.diagnostic_report import (
    EXPECTED_METRIC_KEYS,
    METRIC_CYCLE_TIME_COMPLETED_COUNT,
    METRIC_REOPEN_EVENT_COUNT,
    METRIC_SCOPE_LATE_CHILD_COUNT,
    METRIC_SCOPE_SPEC_CHANGE_COUNT,
    METRIC_SPEC_QUALITY_ISSUE_COUNT,
    MetricFamily,
    generate_diagnostic_report,
)
from throughline.config import settings
from throughline.db.models import (
    DiagnosticReport,
    DiagnosticReportStatus,
    EstimationAccuracyAggregate,
    EstimationAccuracyIssueMetric,
    Issue,
    IssueFieldChange,
    IssueTransition,
    LateChildEvent,
    Membership,
    MembershipRole,
    Org,
    Outcome,
    ReopenAggregate,
    ReopenEvent,
    ScopeChangeAggregate,
    SpecChangeEvent,
    SpecQualityAggregate,
    SpecQualityIssueIndicator,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.tenancy import use_org
from throughline.workers.jobs import generate_diagnostic_report_job
from throughline.workers.settings import WorkerSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
RANGE_START = date(2024, 6, 1)
RANGE_END = date(2024, 6, 30)


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_database_url(settings.database_url))
    return cfg


def _postgres_reachable() -> bool:
    url = sqlalchemy_database_url(settings.database_url)
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url.replace("postgresql+psycopg", "postgresql", 1))
        host = parsed.hostname or "localhost"
        port = parsed.port or 5432
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def migrated_db() -> None:
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose)")
    try:
        with get_session_factory()() as session:
            session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        pytest.skip("Postgres not reachable (run via docker compose)")
    command.upgrade(_alembic_config(), "head")


@pytest.fixture
def db_session(migrated_db: None):
    Session = get_session_factory()
    with Session() as session:
        yield session


def _wipe(db_session) -> None:
    db_session.execute(delete(DiagnosticReport))
    db_session.execute(delete(EstimationAccuracyAggregate))
    db_session.execute(delete(EstimationAccuracyIssueMetric))
    db_session.execute(delete(SpecQualityAggregate))
    db_session.execute(delete(SpecQualityIssueIndicator))
    db_session.execute(delete(ScopeChangeAggregate))
    db_session.execute(delete(SpecChangeEvent))
    db_session.execute(delete(LateChildEvent))
    db_session.execute(delete(IssueFieldChange))
    db_session.execute(delete(ReopenAggregate))
    db_session.execute(delete(ReopenEvent))
    db_session.execute(delete(Outcome))
    db_session.execute(delete(IssueTransition))
    db_session.execute(delete(Issue))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Diagnostic Report Org")
    user = User(auth_subject="report-admin", email="report@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2024, 6, day, hour, 0, tzinfo=UTC)


def _transition(
    org_id,
    key: str,
    when: datetime,
    from_status: str | None,
    to_status: str,
    *,
    event_id: str,
    index: int = 0,
) -> IssueTransition:
    return IssueTransition(
        org_id=org_id,
        external_key=key,
        transitioned_at=when,
        from_status=from_status,
        to_status=to_status,
        actor_id=None,
        actor_display_name=None,
        external_event_id=event_id,
        event_index=index,
    )


def _seed_fixture_org(db_session, org: Org) -> None:
    """Canonical fixture covering cycle time, reopen, scope, spec, estimation."""
    estimate_seconds = int(timedelta(days=2).total_seconds())
    db_session.add_all(
        [
            Issue(
                org_id=org.id,
                external_key="STORY-1",
                project_key="PROJ",
                epic_key="EPIC-1",
                team_key="Platform",
                summary="Completed with reopen",
                status="Done",
                issue_type="Story",
                description="A reasonably long description for indicators.",
                acceptance_criteria=None,
                story_points=None,
                original_estimate_seconds=estimate_seconds,
                source_created_at=_at(1),
                source_updated_at=_at(10),
            ),
            Issue(
                org_id=org.id,
                external_key="CHILD-EARLY",
                project_key="PROJ",
                epic_key="EPIC-1",
                team_key="Platform",
                summary="Early epic child",
                status="In Progress",
                issue_type="Story",
                description="early",
                source_created_at=_at(2),
                source_updated_at=_at(5),
            ),
            Issue(
                org_id=org.id,
                external_key="CHILD-LATE",
                project_key="PROJ",
                epic_key="EPIC-1",
                team_key="Platform",
                summary="Late epic child",
                status="To Do",
                issue_type="Story",
                description="late",
                source_created_at=_at(20),
                source_updated_at=_at(20),
            ),
            Issue(
                org_id=org.id,
                external_key="SPEC-1",
                project_key="PROJ",
                epic_key="EPIC-1",
                team_key="Platform",
                summary="Spec edit after start",
                status="In Progress",
                issue_type="Story",
                description="edited",
                acceptance_criteria="Given When Then",
                source_created_at=_at(3),
                source_updated_at=_at(12),
            ),
        ]
    )
    db_session.add_all(
        [
            _transition(org.id, "STORY-1", _at(4), None, "To Do", event_id="1"),
            _transition(org.id, "STORY-1", _at(5), "To Do", "In Progress", event_id="2"),
            _transition(org.id, "STORY-1", _at(7), "In Progress", "Done", event_id="3"),
            _transition(org.id, "STORY-1", _at(8), "Done", "In Progress", event_id="4"),
            _transition(org.id, "STORY-1", _at(10), "In Progress", "Done", event_id="5"),
            _transition(
                org.id, "CHILD-EARLY", _at(5), "To Do", "In Progress", event_id="c1"
            ),
            _transition(org.id, "SPEC-1", _at(6), "To Do", "In Progress", event_id="s1"),
        ]
    )
    db_session.add(
        IssueFieldChange(
            org_id=org.id,
            external_key="SPEC-1",
            changed_at=_at(12),
            field="acceptance_criteria",
            external_event_id="ac1",
            event_index=0,
        )
    )
    db_session.commit()


def test_diagnostic_reports_table_migrated(migrated_db) -> None:
    inspector = inspect(get_engine())
    assert "diagnostic_reports" in inspector.get_table_names()
    cols = {c["name"] for c in inspector.get_columns("diagnostic_reports")}
    assert {
        "org_id",
        "range_start",
        "range_end",
        "version",
        "status",
        "metrics",
        "generation_detail",
        "generated_at",
        "error_message",
        "deleted_at",
    } <= cols


def test_generate_report_contains_metric_keys_and_evidence(db_session, org_ready):
    org = org_ready
    _seed_fixture_org(db_session, org)

    with use_org(org.id):
        report = generate_diagnostic_report(db_session, org.id, RANGE_START, RANGE_END)
        db_session.commit()

        assert report.status == DiagnosticReportStatus.SUCCESS.value
        assert report.version == 1
        assert report.generated_at is not None
        assert report.error_message is None
        assert set(report.metrics.keys()) == EXPECTED_METRIC_KEYS

        for key in EXPECTED_METRIC_KEYS:
            entry = report.metrics[key]
            assert "value" in entry
            assert "evidence_refs" in entry
            assert isinstance(entry["evidence_refs"], list)

        cycle = report.metrics[METRIC_CYCLE_TIME_COMPLETED_COUNT]
        assert cycle["value"] == 1
        assert "issue:STORY-1/cycle-time" in cycle["evidence_refs"]

        reopen = report.metrics[METRIC_REOPEN_EVENT_COUNT]
        assert reopen["value"] == 1
        assert any("issue:STORY-1/transition:" in r for r in reopen["evidence_refs"])

        late = report.metrics[METRIC_SCOPE_LATE_CHILD_COUNT]
        assert late["value"] == 1
        assert "epic:EPIC-1/late-child:CHILD-LATE" in late["evidence_refs"]

        spec_change = report.metrics[METRIC_SCOPE_SPEC_CHANGE_COUNT]
        assert spec_change["value"] == 1
        assert any("issue:SPEC-1/field:" in r for r in spec_change["evidence_refs"])

        spec_quality = report.metrics[METRIC_SPEC_QUALITY_ISSUE_COUNT]
        assert spec_quality["value"] >= 1
        assert any(r.startswith("issue:") for r in spec_quality["evidence_refs"])

        for family in MetricFamily:
            assert report.generation_detail[family.value]["ok"] is True


def test_rerun_creates_new_version_without_mutating_prior(db_session, org_ready):
    org = org_ready
    _seed_fixture_org(db_session, org)

    with use_org(org.id):
        first = generate_diagnostic_report(db_session, org.id, RANGE_START, RANGE_END)
        db_session.commit()
        first_id = first.id
        first_metrics = dict(first.metrics)
        first_generated_at = first.generated_at

        second = generate_diagnostic_report(db_session, org.id, RANGE_START, RANGE_END)
        db_session.commit()

        assert first.version == 1
        assert second.version == 2
        assert second.id != first_id

        reloaded = db_session.get(DiagnosticReport, first_id)
        assert reloaded is not None
        assert reloaded.metrics == first_metrics
        assert reloaded.generated_at == first_generated_at
        assert reloaded.version == 1

        versions = list(
            db_session.scalars(
                select(DiagnosticReport.version).order_by(DiagnosticReport.version)
            ).all()
        )
        assert versions == [1, 2]


def test_partial_status_when_one_family_fails(db_session, org_ready):
    org = org_ready
    _seed_fixture_org(db_session, org)

    def _reopen_boom(_db, _org_id, **_kwargs):
        raise RuntimeError("reopen boom")

    failing = dict(diagnostic_report_mod._FAMILY_COMPUTE)
    failing[MetricFamily.REOPEN] = _reopen_boom

    with use_org(org.id):
        with patch.object(diagnostic_report_mod, "_FAMILY_COMPUTE", failing):
            report = generate_diagnostic_report(
                db_session, org.id, RANGE_START, RANGE_END
            )
            db_session.commit()

        assert report.status == DiagnosticReportStatus.PARTIAL.value
        assert report.error_message is not None
        assert "reopen" in report.error_message
        assert report.generation_detail["reopen"]["ok"] is False
        assert report.generation_detail["cycle_time"]["ok"] is True
        # Successful families still contribute metrics; reopen keys absent.
        assert METRIC_CYCLE_TIME_COMPLETED_COUNT in report.metrics
        assert METRIC_REOPEN_EVENT_COUNT not in report.metrics


def test_failed_status_when_all_families_fail(db_session, org_ready):
    org = org_ready
    _seed_fixture_org(db_session, org)

    def _boom(_db, _org_id, **_kwargs):
        raise RuntimeError("family boom")

    failing = {family: _boom for family in MetricFamily}
    with use_org(org.id):
        with patch.object(diagnostic_report_mod, "_FAMILY_COMPUTE", failing):
            report = generate_diagnostic_report(
                db_session, org.id, RANGE_START, RANGE_END
            )
            db_session.commit()

        assert report.status == DiagnosticReportStatus.FAILED.value
        assert report.error_message is not None
        assert report.metrics == {}


def test_arq_job_generates_report_end_to_end(db_session, org_ready):
    org = org_ready
    _seed_fixture_org(db_session, org)

    names = {
        getattr(fn, "name", getattr(fn, "__name__", None))
        for fn in WorkerSettings.functions
    }
    assert "generate_diagnostic_report" in names

    result = asyncio.run(
        generate_diagnostic_report_job(
            {},
            str(org.id),
            RANGE_START.isoformat(),
            RANGE_END.isoformat(),
        )
    )
    assert result["ok"] is True
    assert result["status"] == DiagnosticReportStatus.SUCCESS.value
    assert result["version"] == 1
    assert set(result["metric_keys"]) == EXPECTED_METRIC_KEYS

    with use_org(org.id):
        row = db_session.scalar(select(DiagnosticReport))
        assert row is not None
        assert str(row.id) == result["report_id"]
        assert set(row.metrics.keys()) == EXPECTED_METRIC_KEYS
        assert all(
            isinstance(entry.get("evidence_refs"), list) for entry in row.metrics.values()
        )