"""Reopen / rework detection from hand-constructed transitions (issue #18)."""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics.reopen import (
    AggregateDimension,
    IssueDimensions,
    aggregate_detections,
    compute_and_store_reopens_for_org,
    detect_reopens,
    is_reopen_transition,
    month_period_start,
)
from throughline.analytics.status import StatusLane, status_map_classifier
from throughline.config import settings
from throughline.db.models import (
    Issue,
    IssueTransition,
    Membership,
    MembershipRole,
    Org,
    ReopenAggregate,
    ReopenEvent,
    User,
)
from throughline.db.session import get_session_factory, sqlalchemy_database_url
from throughline.domain.issues import CanonicalTransition
from throughline.tenancy import use_org

REPO_ROOT = Path(__file__).resolve().parents[1]

CLASSIFY = status_map_classifier(
    {
        "To Do": StatusLane.TODO,
        "In Progress": StatusLane.IN_PROGRESS,
        "Done": StatusLane.DONE,
    }
)


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
    db_session.execute(delete(ReopenAggregate))
    db_session.execute(delete(ReopenEvent))
    db_session.execute(delete(IssueTransition))
    db_session.execute(delete(Issue))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Reopen Org")
    user = User(auth_subject="reopen-admin-sub", email="reopen@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org


def _at(day: int, hour: int = 12, *, month: int = 1) -> datetime:
    return datetime(2024, month, day, hour, 0, tzinfo=UTC)


def _t(
    key: str,
    at: datetime,
    from_status: str | None,
    to_status: str | None,
    *,
    event_id: str,
    index: int = 0,
) -> CanonicalTransition:
    return CanonicalTransition(
        external_key=key,
        transitioned_at=at,
        from_status=from_status,
        to_status=to_status,
        actor_id="u1",
        actor_display_name="Alice",
        external_event_id=event_id,
        event_index=index,
    )


def test_is_reopen_done_to_active_only():
    assert is_reopen_transition("Done", "In Progress", classify=CLASSIFY)
    assert is_reopen_transition("Done", "To Do", classify=CLASSIFY)
    assert not is_reopen_transition("In Progress", "Done", classify=CLASSIFY)
    assert not is_reopen_transition("Done", "Done", classify=CLASSIFY)
    assert not is_reopen_transition("To Do", "In Progress", classify=CLASSIFY)


def test_single_reopen_detection_and_evidence():
    """One Done → In Progress yields one detection with a stable evidence ref."""
    dims = IssueDimensions(project_key="PROJ", epic_key="EPIC-1")
    seq = [
        _t("STORY-1", _at(1), None, "To Do", event_id="1"),
        _t("STORY-1", _at(2), "To Do", "In Progress", event_id="2"),
        _t("STORY-1", _at(3), "In Progress", "Done", event_id="3"),
        _t("STORY-1", _at(4), "Done", "In Progress", event_id="4"),
        _t("STORY-1", _at(5), "In Progress", "Done", event_id="5"),
    ]
    detections = detect_reopens(seq, dimensions=dims, classify=CLASSIFY)

    assert len(detections) == 1
    d = detections[0]
    assert d.external_key == "STORY-1"
    assert d.from_status == "Done"
    assert d.to_status == "In Progress"
    assert d.evidence_external_event_id == "4"
    assert d.evidence_event_index == 0
    assert d.evidence_ref == "issue:STORY-1/transition:4:0"
    assert d.project_key == "PROJ"
    assert d.epic_key == "EPIC-1"
    assert "cause" not in d.evidence_ref.lower()
    assert "blame" not in d.evidence_ref.lower()


def test_multiple_reopens():
    """Two separate done→active moves produce two detections."""
    seq = [
        _t("STORY-2", _at(1), None, "In Progress", event_id="1"),
        _t("STORY-2", _at(2), "In Progress", "Done", event_id="2"),
        _t("STORY-2", _at(3), "Done", "To Do", event_id="3"),
        _t("STORY-2", _at(4), "To Do", "In Progress", event_id="4"),
        _t("STORY-2", _at(5), "In Progress", "Done", event_id="5"),
        _t("STORY-2", _at(6), "Done", "In Progress", event_id="6"),
    ]
    detections = detect_reopens(seq, classify=CLASSIFY)
    assert len(detections) == 2
    assert [d.evidence_external_event_id for d in detections] == ["3", "6"]


def test_never_reopened_done_issue():
    """Happy-path done with no return to active → zero detections."""
    seq = [
        _t("STORY-3", _at(1), None, "To Do", event_id="1"),
        _t("STORY-3", _at(2), "To Do", "In Progress", event_id="2"),
        _t("STORY-3", _at(4), "In Progress", "Done", event_id="3"),
    ]
    assert detect_reopens(seq, classify=CLASSIFY) == []


def test_aggregate_by_epic_project_and_period():
    dims_a = IssueDimensions(project_key="PROJ", epic_key="EPIC-1")
    dims_b = IssueDimensions(project_key="PROJ", epic_key="EPIC-2")
    jan = [
        *_detections_for(
            "A-1",
            [_at(5, month=1)],
            dims_a,
        ),
        *_detections_for(
            "A-2",
            [_at(10, month=1), _at(15, month=1)],
            dims_b,
        ),
    ]
    feb = _detections_for("A-3", [_at(2, month=2)], dims_a)
    rows = aggregate_detections([*jan, *feb])

    by = {(period, dim, key): count for period, dim, key, count in rows}
    assert by[(date(2024, 1, 1), AggregateDimension.PROJECT, "PROJ")] == 3
    assert by[(date(2024, 1, 1), AggregateDimension.EPIC, "EPIC-1")] == 1
    assert by[(date(2024, 1, 1), AggregateDimension.EPIC, "EPIC-2")] == 2
    assert by[(date(2024, 2, 1), AggregateDimension.PROJECT, "PROJ")] == 1
    assert by[(date(2024, 2, 1), AggregateDimension.EPIC, "EPIC-1")] == 1
    assert month_period_start(_at(15, month=1)) == date(2024, 1, 1)


def _detections_for(key: str, ats: list[datetime], dims: IssueDimensions):
    out = []
    for i, at in enumerate(ats, start=1):
        seq = [
            _t(key, at, "Done", "In Progress", event_id=str(i)),
        ]
        out.extend(detect_reopens(seq, dimensions=dims, classify=CLASSIFY))
    return out


def test_store_reopens_idempotent_and_org_scoped(db_session, org_ready):
    org = org_ready
    other = Org(name="Other Reopen Org")
    db_session.add(other)
    db_session.flush()

    with use_org(org.id):
        db_session.add(
            Issue(
                org_id=org.id,
                external_key="STORY-10",
                project_key="PROJ",
                epic_key="EPIC-9",
            )
        )
        for t in [
            _t("STORY-10", _at(1), None, "In Progress", event_id="1"),
            _t("STORY-10", _at(2), "In Progress", "Done", event_id="2"),
            _t("STORY-10", _at(3), "Done", "In Progress", event_id="3"),
            _t("STORY-10", _at(4), "In Progress", "Done", event_id="4"),
        ]:
            db_session.add(
                IssueTransition(
                    org_id=org.id,
                    external_key=t.external_key,
                    transitioned_at=t.transitioned_at,
                    from_status=t.from_status,
                    to_status=t.to_status,
                    actor_id=t.actor_id,
                    actor_display_name=t.actor_display_name,
                    external_event_id=t.external_event_id,
                    event_index=t.event_index,
                )
            )
        db_session.commit()

        n1, a1 = compute_and_store_reopens_for_org(db_session, org.id, classify=CLASSIFY)
        db_session.commit()
        assert n1 == 1
        assert a1 >= 2  # project + epic slices for the month

        events = list(db_session.scalars(select(ReopenEvent)).all())
        assert len(events) == 1
        event_id = events[0].id
        assert events[0].evidence_ref == "issue:STORY-10/transition:3:0"
        assert events[0].project_key == "PROJ"
        assert events[0].epic_key == "EPIC-9"
        # Frequency only — model has no cause/blame columns.
        assert not hasattr(events[0], "cause")
        assert not hasattr(events[0], "blame")
        assert not hasattr(events[0], "root_cause")

        aggs = list(db_session.scalars(select(ReopenAggregate)).all())
        assert {(r.dimension, r.dimension_key, r.reopen_count) for r in aggs} == {
            ("project", "PROJ", 1),
            ("epic", "EPIC-9", 1),
        }

        n2, a2 = compute_and_store_reopens_for_org(db_session, org.id, classify=CLASSIFY)
        db_session.commit()
        assert n2 == 1
        assert a2 == a1
        again = list(db_session.scalars(select(ReopenEvent)).all())
        assert len(again) == 1
        assert again[0].id == event_id

    with use_org(other.id):
        assert db_session.scalars(select(ReopenEvent)).all() == []
        assert db_session.scalars(select(ReopenAggregate)).all() == []


def test_store_never_reopened_creates_no_events(db_session, org_ready):
    org = org_ready
    with use_org(org.id):
        db_session.add(Issue(org_id=org.id, external_key="STORY-11", project_key="PROJ"))
        for t in [
            _t("STORY-11", _at(1), None, "To Do", event_id="1"),
            _t("STORY-11", _at(2), "To Do", "In Progress", event_id="2"),
            _t("STORY-11", _at(3), "In Progress", "Done", event_id="3"),
        ]:
            db_session.add(
                IssueTransition(
                    org_id=org.id,
                    external_key=t.external_key,
                    transitioned_at=t.transitioned_at,
                    from_status=t.from_status,
                    to_status=t.to_status,
                    actor_id=t.actor_id,
                    actor_display_name=t.actor_display_name,
                    external_event_id=t.external_event_id,
                    event_index=t.event_index,
                )
            )
        db_session.commit()

        n, a = compute_and_store_reopens_for_org(db_session, org.id, classify=CLASSIFY)
        db_session.commit()
        assert n == 0
        assert a == 0
        assert db_session.scalars(select(ReopenEvent)).all() == []
        assert db_session.scalars(select(ReopenAggregate)).all() == []
