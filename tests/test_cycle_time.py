"""Cycle time computation from hand-constructed transition sequences (issue #17)."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics.cycle_time import (
    compute_and_store_outcomes_for_org,
    compute_cycle_time,
    store_outcome,
)
from throughline.analytics.status import StatusLane, status_map_classifier
from throughline.config import settings
from throughline.db.models import IssueTransition, Membership, MembershipRole, Org, Outcome, User
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
    db_session.execute(delete(Outcome))
    db_session.execute(delete(IssueTransition))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Cycle Time Org")
    user = User(auth_subject="cycle-admin-sub", email="cycle@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2024, 1, day, hour, 0, tzinfo=UTC)


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


def test_happy_path_time_in_status_and_cycle_time():
    """To Do → In Progress → Done: one pass, closed intervals only."""
    seq = [
        _t("STORY-1", _at(1), None, "To Do", event_id="1"),
        _t("STORY-1", _at(2), "To Do", "In Progress", event_id="2"),
        _t("STORY-1", _at(5), "In Progress", "Done", event_id="3"),
    ]
    result = compute_cycle_time(seq, classify=CLASSIFY)

    assert result.time_in_status_seconds == {
        "To Do": int(timedelta(days=1).total_seconds()),
        "In Progress": int(timedelta(days=3).total_seconds()),
    }
    assert result.cycle_time_seconds == int(timedelta(days=3).total_seconds())
    assert len(result.passes) == 1
    assert result.first_in_progress_at == _at(2)
    assert result.first_done_at == _at(5)
    assert result.last_done_at == _at(5)


def test_reopen_starts_second_pass():
    """Done → In Progress → Done is a reopen; two passes; cycle_time is first pass."""
    seq = [
        _t("STORY-2", _at(1), None, "To Do", event_id="1"),
        _t("STORY-2", _at(2), "To Do", "In Progress", event_id="2"),
        _t("STORY-2", _at(4), "In Progress", "Done", event_id="3"),
        _t("STORY-2", _at(5), "Done", "In Progress", event_id="4"),
        _t("STORY-2", _at(6), "In Progress", "Done", event_id="5"),
    ]
    result = compute_cycle_time(seq, classify=CLASSIFY)

    assert len(result.passes) == 2
    assert result.cycle_time_seconds == int(timedelta(days=2).total_seconds())
    assert result.passes[0].duration_seconds == int(timedelta(days=2).total_seconds())
    assert result.passes[1].duration_seconds == int(timedelta(days=1).total_seconds())
    assert result.first_in_progress_at == _at(2)
    assert result.first_done_at == _at(4)
    assert result.last_done_at == _at(6)
    assert result.time_in_status_seconds["Done"] == int(timedelta(days=1).total_seconds())


def test_multiple_passes_without_todo_between():
    """Two distinct in-progress→done completions after a reopen via To Do."""
    seq = [
        _t("STORY-3", _at(1), None, "In Progress", event_id="1"),
        _t("STORY-3", _at(3), "In Progress", "Done", event_id="2"),
        _t("STORY-3", _at(4), "Done", "To Do", event_id="3"),
        _t("STORY-3", _at(5), "To Do", "In Progress", event_id="4"),
        _t("STORY-3", _at(8), "In Progress", "Done", event_id="5"),
    ]
    result = compute_cycle_time(seq, classify=CLASSIFY)

    assert len(result.passes) == 2
    assert result.cycle_time_seconds == int(timedelta(days=2).total_seconds())
    assert result.passes[1].duration_seconds == int(timedelta(days=3).total_seconds())


def test_backwards_move_keeps_pass_open():
    """In Progress → To Do (backwards) does not cancel the open pass.

    Calendar cycle time continues until Done; time-in-status still accumulates.
    """
    seq = [
        _t("STORY-4", _at(1), None, "To Do", event_id="1"),
        _t("STORY-4", _at(2), "To Do", "In Progress", event_id="2"),
        _t("STORY-4", _at(3), "In Progress", "To Do", event_id="3"),
        _t("STORY-4", _at(4), "To Do", "In Progress", event_id="4"),
        _t("STORY-4", _at(6), "In Progress", "Done", event_id="5"),
    ]
    result = compute_cycle_time(seq, classify=CLASSIFY)

    assert len(result.passes) == 1
    # Pass opened at day 2, closed at day 6 (includes backwards detour).
    assert result.cycle_time_seconds == int(timedelta(days=4).total_seconds())
    assert result.time_in_status_seconds == {
        "To Do": int(timedelta(days=2).total_seconds()),  # day1→2 and day3→4
        "In Progress": int(timedelta(days=3).total_seconds()),  # day2→3 and day4→6
    }


def test_out_of_order_workflow_and_clamped_durations_do_not_crash():
    """Skip/backwards lanes and equal timestamps: no crash; durations ≥ 0."""
    same = _at(3)
    seq = [
        _t("STORY-5", _at(1), None, "Done", event_id="1"),  # landed in Done first
        _t("STORY-5", same, "Done", "To Do", event_id="2", index=0),
        _t("STORY-5", same, "To Do", "In Progress", event_id="2", index=1),
        _t("STORY-5", _at(5), "In Progress", "Done", event_id="3"),
    ]
    result = compute_cycle_time(seq, classify=CLASSIFY)

    assert all(v >= 0 for v in result.time_in_status_seconds.values())
    assert result.cycle_time_seconds == int(timedelta(days=2).total_seconds())
    assert len(result.passes) == 1


def test_store_outcomes_org_scoped_and_idempotent(db_session, org_ready):
    org = org_ready
    other = Org(name="Other Org")
    db_session.add(other)
    db_session.flush()

    seq = [
        _t("STORY-10", _at(1), None, "To Do", event_id="1"),
        _t("STORY-10", _at(2), "To Do", "In Progress", event_id="2"),
        _t("STORY-10", _at(4), "In Progress", "Done", event_id="3"),
    ]

    with use_org(org.id):
        for t in seq:
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

        n1 = compute_and_store_outcomes_for_org(db_session, org.id, classify=CLASSIFY)
        db_session.commit()
        assert n1 == 1
        row = db_session.scalar(select(Outcome).where(Outcome.external_key == "STORY-10"))
        assert row is not None
        assert row.cycle_time_seconds == int(timedelta(days=2).total_seconds())
        assert row.time_in_status_seconds["In Progress"] == int(
            timedelta(days=2).total_seconds()
        )
        fingerprint = row.transitions_fingerprint
        row_id = row.id

        n2 = compute_and_store_outcomes_for_org(db_session, org.id, classify=CLASSIFY)
        db_session.commit()
        assert n2 == 1
        again = db_session.scalar(select(Outcome).where(Outcome.external_key == "STORY-10"))
        assert again is not None
        assert again.id == row_id
        assert again.transitions_fingerprint == fingerprint

    with use_org(other.id):
        assert db_session.scalars(select(Outcome)).all() == []


def test_store_outcome_updates_when_transitions_change(db_session, org_ready):
    org = org_ready
    with use_org(org.id):
        first = compute_cycle_time(
            [
                _t("STORY-11", _at(1), None, "In Progress", event_id="1"),
                _t("STORY-11", _at(2), "In Progress", "Done", event_id="2"),
            ],
            classify=CLASSIFY,
        )
        row = store_outcome(db_session, first)
        db_session.commit()
        assert row.cycle_time_seconds == int(timedelta(days=1).total_seconds())

        second = compute_cycle_time(
            [
                _t("STORY-11", _at(1), None, "In Progress", event_id="1"),
                _t("STORY-11", _at(3), "In Progress", "Done", event_id="2"),
            ],
            classify=CLASSIFY,
        )
        updated = store_outcome(db_session, second)
        db_session.commit()
        assert updated.id == row.id
        assert updated.cycle_time_seconds == int(timedelta(days=2).total_seconds())
        assert updated.transitions_fingerprint != first.transitions_fingerprint
