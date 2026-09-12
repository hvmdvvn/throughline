"""Estimation accuracy: estimate vs cycle time, coverage, team aggregates (issue #21)."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics.estimation_accuracy import (
    AccuracyBucket,
    EstimateKind,
    EstimationAccuracyDimension,
    accuracy_bucket_for_ratio,
    aggregate_estimation_accuracy,
    compute_and_store_estimation_accuracy_for_org,
    detect_estimation_accuracy,
    detect_estimation_accuracy_for_issue,
    has_usable_estimate,
)
from throughline.config import settings
from throughline.db.models import (
    EstimationAccuracyAggregate,
    EstimationAccuracyIssueMetric,
    Issue,
    Membership,
    MembershipRole,
    Org,
    Outcome,
    User,
)
from throughline.db.session import get_session_factory, sqlalchemy_database_url
from throughline.domain.issues import CanonicalIssue
from throughline.tenancy import use_org

REPO_ROOT = Path(__file__).resolve().parents[1]
DAY = int(timedelta(days=1).total_seconds())


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
    db_session.execute(delete(EstimationAccuracyAggregate))
    db_session.execute(delete(EstimationAccuracyIssueMetric))
    db_session.execute(delete(Outcome))
    db_session.execute(delete(Issue))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Estimation Accuracy Org")
    user = User(auth_subject="est-acc-admin", email="est@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org


def _issue(
    key: str,
    *,
    team: str | None = "Platform",
    story_points: float | None = None,
    original_estimate_seconds: int | None = None,
    when: datetime | None = None,
) -> CanonicalIssue:
    at = when or datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    return CanonicalIssue(
        external_key=key,
        project_key="PROJ",
        summary=f"Summary {key}",
        status="Done",
        issue_type="Story",
        acceptance_criteria=None,
        story_points=story_points,
        created_at=at,
        updated_at=at,
        epic_key=None,
        description=None,
        team_key=team,
        original_estimate_seconds=original_estimate_seconds,
    )


def test_has_usable_estimate_and_bucket_thresholds():
    assert has_usable_estimate(None, None) is False
    assert has_usable_estimate(0, None) is False
    assert has_usable_estimate(-1, 0) is False
    assert has_usable_estimate(3.0, None) is True
    assert has_usable_estimate(None, DAY) is True

    assert accuracy_bucket_for_ratio(None) == AccuracyBucket.UNCOMPARABLE
    assert accuracy_bucket_for_ratio(0.5) == AccuracyBucket.OVER
    assert accuracy_bucket_for_ratio(1.0) == AccuracyBucket.ACCURATE
    assert accuracy_bucket_for_ratio(2.0) == AccuracyBucket.UNDER


def test_estimated_vs_actual_original_estimate():
    """Original estimate vs cycle time: over / under / accurate."""
    over = detect_estimation_accuracy_for_issue(
        _issue("E-1", original_estimate_seconds=4 * DAY),
        cycle_time_seconds=2 * DAY,
    )
    under = detect_estimation_accuracy_for_issue(
        _issue("E-2", original_estimate_seconds=2 * DAY),
        cycle_time_seconds=4 * DAY,
    )
    accurate = detect_estimation_accuracy_for_issue(
        _issue("E-3", original_estimate_seconds=3 * DAY),
        cycle_time_seconds=3 * DAY,
    )

    assert over.estimate_kind == EstimateKind.ORIGINAL_ESTIMATE
    assert over.accuracy_bucket == AccuracyBucket.OVER
    assert over.accuracy_ratio == pytest.approx(0.5)

    assert under.accuracy_bucket == AccuracyBucket.UNDER
    assert under.accuracy_ratio == pytest.approx(2.0)

    assert accurate.accuracy_bucket == AccuracyBucket.ACCURATE
    assert accurate.accuracy_ratio == pytest.approx(1.0)
    assert accurate.evidence_ref == "issue:E-3/estimation-accuracy"


def test_story_points_vs_cycle_time_with_team_calibration():
    """Story points convert via team median seconds/point, then compare."""
    issues = [
        _issue("SP-1", team="Alpha", story_points=2.0),
        _issue("SP-2", team="Alpha", story_points=4.0),
        _issue("SP-3", team="Alpha", story_points=2.0),
    ]
    # Median seconds/point = median(1d, 1d, 3d) = 1d → SP-3 expected 2d, actual 6d → under
    cycle_times = {
        "SP-1": 2 * DAY,
        "SP-2": 4 * DAY,
        "SP-3": 6 * DAY,
    }
    detections = detect_estimation_accuracy(issues, cycle_times)
    by_key = {d.external_key: d for d in detections}

    assert by_key["SP-1"].estimate_kind == EstimateKind.STORY_POINTS
    assert by_key["SP-1"].seconds_per_point == pytest.approx(float(DAY))
    assert by_key["SP-1"].accuracy_bucket == AccuracyBucket.ACCURATE
    assert by_key["SP-3"].accuracy_bucket == AccuracyBucket.UNDER
    assert by_key["SP-3"].accuracy_ratio == pytest.approx(3.0)


def test_missing_estimates_reduce_coverage_only():
    """Missing estimates do not crash; they lower coverage and stay uncomparable."""
    issues = [
        _issue("M-1", team="Beta", original_estimate_seconds=DAY),
        _issue("M-2", team="Beta", story_points=None, original_estimate_seconds=None),
        _issue("M-3", team="Beta", story_points=0, original_estimate_seconds=None),
    ]
    cycle_times = {"M-1": DAY, "M-2": 2 * DAY, "M-3": 3 * DAY}
    detections = detect_estimation_accuracy(issues, cycle_times)
    aggs = aggregate_estimation_accuracy(detections)

    assert len(aggs) == 1
    row = aggs[0]
    assert row.dimension == EstimationAccuracyDimension.TEAM
    assert row.dimension_key == "Beta"
    assert row.issue_count == 3
    assert row.with_estimate_count == 1
    assert row.coverage == pytest.approx(1 / 3)
    assert row.comparable_count == 1
    assert row.accurate_count == 1
    assert {d.external_key for d in detections if not d.has_usable_estimate} == {
        "M-2",
        "M-3",
    }
    assert all(
        d.accuracy_bucket == AccuracyBucket.UNCOMPARABLE
        for d in detections
        if d.external_key in {"M-2", "M-3"}
    )


def test_team_aggregation_without_user_level_breakdown():
    """Aggregates are team-only; no person/actor fields on detections or rows."""
    issues = [
        _issue("T-1", team="Platform", original_estimate_seconds=2 * DAY),
        _issue("T-2", team="Platform", original_estimate_seconds=2 * DAY),
        _issue("T-3", team="Mobile", original_estimate_seconds=DAY),
    ]
    cycle_times = {
        "T-1": DAY,  # over
        "T-2": 4 * DAY,  # under
        "T-3": DAY,  # accurate
    }
    detections = detect_estimation_accuracy(issues, cycle_times)
    aggs = aggregate_estimation_accuracy(detections)

    assert {a.dimension_key for a in aggs} == {"Platform", "Mobile"}
    assert all(a.dimension == EstimationAccuracyDimension.TEAM for a in aggs)

    platform = next(a for a in aggs if a.dimension_key == "Platform")
    assert platform.over_count == 1
    assert platform.under_count == 1
    assert platform.accurate_count == 0
    assert platform.coverage == pytest.approx(1.0)

    # Guard: no person-shaped fields on public detection / aggregate shapes.
    for d in detections:
        assert not hasattr(d, "actor_id")
        assert not hasattr(d, "user_id")
        assert not hasattr(d, "assignee")
        assert not hasattr(d, "person_key")
    for a in aggs:
        assert a.dimension.value == "team"
        assert not hasattr(a, "user_id")
        assert not hasattr(a, "actor_id")


def test_compute_and_store_persists_team_aggregates_and_coverage(org_ready, db_session):
    org = org_ready
    with use_org(org.id):
        db_session.add_all(
            [
                Issue(
                    org_id=org.id,
                    external_key="DB-1",
                    project_key="PROJ",
                    team_key="Platform",
                    summary="Estimated",
                    status="Done",
                    issue_type="Story",
                    story_points=None,
                    original_estimate_seconds=2 * DAY,
                    source_created_at=datetime(2024, 3, 10, tzinfo=UTC),
                    source_updated_at=datetime(2024, 3, 10, tzinfo=UTC),
                ),
                Issue(
                    org_id=org.id,
                    external_key="DB-2",
                    project_key="PROJ",
                    team_key="Platform",
                    summary="No estimate",
                    status="Done",
                    issue_type="Story",
                    story_points=None,
                    original_estimate_seconds=None,
                    source_created_at=datetime(2024, 3, 12, tzinfo=UTC),
                    source_updated_at=datetime(2024, 3, 12, tzinfo=UTC),
                ),
                Outcome(
                    org_id=org.id,
                    external_key="DB-1",
                    time_in_status_seconds={},
                    cycle_time_seconds=DAY,
                    pass_count=1,
                    cycle_passes=[],
                    first_done_at=datetime(2024, 3, 10, tzinfo=UTC),
                    transitions_fingerprint="fp-db-1",
                ),
                Outcome(
                    org_id=org.id,
                    external_key="DB-2",
                    time_in_status_seconds={},
                    cycle_time_seconds=3 * DAY,
                    pass_count=1,
                    cycle_passes=[],
                    first_done_at=datetime(2024, 3, 12, tzinfo=UTC),
                    transitions_fingerprint="fp-db-2",
                ),
            ]
        )
        db_session.commit()

        metric_count, agg_count = compute_and_store_estimation_accuracy_for_org(
            db_session, org.id
        )
        db_session.commit()

        assert metric_count == 2
        assert agg_count == 1

        metrics = list(db_session.scalars(select(EstimationAccuracyIssueMetric)).all())
        assert {m.external_key for m in metrics} == {"DB-1", "DB-2"}
        by_key = {m.external_key: m for m in metrics}
        assert by_key["DB-1"].accuracy_bucket == AccuracyBucket.OVER.value
        assert by_key["DB-2"].has_usable_estimate is False
        assert by_key["DB-2"].accuracy_bucket == AccuracyBucket.UNCOMPARABLE.value

        # Schema / ORM must not expose person-level accuracy columns.
        metric_cols = set(inspect(EstimationAccuracyIssueMetric).columns.keys())
        agg_cols = set(inspect(EstimationAccuracyAggregate).columns.keys())
        forbidden = {"actor_id", "user_id", "assignee", "person_key", "reporter"}
        assert not (metric_cols & forbidden)
        assert not (agg_cols & forbidden)

        aggs = list(db_session.scalars(select(EstimationAccuracyAggregate)).all())
        assert len(aggs) == 1
        assert aggs[0].dimension == EstimationAccuracyDimension.TEAM.value
        assert aggs[0].dimension_key == "Platform"
        assert aggs[0].issue_count == 2
        assert aggs[0].with_estimate_count == 1
        assert aggs[0].coverage == pytest.approx(0.5)
        assert aggs[0].over_count == 1
        assert set(aggs[0].evidence_issue_keys) == {"DB-1", "DB-2"}
