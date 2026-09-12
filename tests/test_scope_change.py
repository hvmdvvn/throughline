"""Scope change metrics: late children + post-start description/AC (issue #19)."""

from __future__ import annotations

import socket
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics.reopen import AggregateDimension, IssueDimensions
from throughline.analytics.scope_change import (
    EpicChild,
    LateChildDetection,
    SpecChangeDetection,
    aggregate_scope_signals,
    compute_and_store_scope_change_for_org,
    detect_late_children,
    detect_spec_changes,
    epic_started_at,
    measure_epic_scope,
)
from throughline.analytics.status import StatusLane, status_map_classifier
from throughline.config import settings
from throughline.db.models import (
    Issue,
    IssueFieldChange,
    IssueTransition,
    LateChildEvent,
    Membership,
    MembershipRole,
    Org,
    ScopeChangeAggregate,
    SpecChangeEvent,
    User,
)
from throughline.db.session import get_session_factory, sqlalchemy_database_url
from throughline.domain.issues import CanonicalFieldChange, CanonicalTransition
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
    db_session.execute(delete(ScopeChangeAggregate))
    db_session.execute(delete(SpecChangeEvent))
    db_session.execute(delete(LateChildEvent))
    db_session.execute(delete(IssueFieldChange))
    db_session.execute(delete(IssueTransition))
    db_session.execute(delete(Issue))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Scope Org")
    user = User(auth_subject="scope-admin-sub", email="scope@example.com")
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


def _fc(
    key: str,
    at: datetime,
    field: str,
    *,
    event_id: str,
    index: int = 0,
) -> CanonicalFieldChange:
    return CanonicalFieldChange(
        external_key=key,
        changed_at=at,
        field=field,
        external_event_id=event_id,
        event_index=index,
    )


def test_child_added_before_start_not_counted():
    """Child created before (or at) epic start is not late scope."""
    children = [
        EpicChild("C-1", "EPIC-1", _at(1), project_key="PROJ"),
        EpicChild("C-2", "EPIC-1", _at(5), project_key="PROJ"),
    ]
    transitions = {
        "C-1": [_t("C-1", _at(10), "To Do", "In Progress", event_id="1")],
        "C-2": [],
    }
    started = epic_started_at(transitions, classify=CLASSIFY)
    assert started == _at(10)

    late = detect_late_children(children, started)
    assert late == []

    result = measure_epic_scope("EPIC-1", children, transitions, classify=CLASSIFY)
    assert result.late_child_count == 0
    assert result.epic_started_at == _at(10)
    assert result.child_count == 2


def test_child_added_after_start_counted():
    """Child created after first sibling entered in-progress is late scope."""
    children = [
        EpicChild("C-1", "EPIC-1", _at(1), project_key="PROJ"),
        EpicChild("C-LATE", "EPIC-1", _at(15), project_key="PROJ"),
    ]
    transitions = {
        "C-1": [_t("C-1", _at(10), "To Do", "In Progress", event_id="1")],
        "C-LATE": [],
    }
    result = measure_epic_scope("EPIC-1", children, transitions, classify=CLASSIFY)

    assert result.late_child_count == 1
    d = result.late_children[0]
    assert d.external_key == "C-LATE"
    assert d.epic_key == "EPIC-1"
    assert d.epic_started_at == _at(10)
    assert d.child_created_at == _at(15)
    assert d.delay_seconds == int((_at(15) - _at(10)).total_seconds())
    assert d.evidence_ref == "epic:EPIC-1/late-child:C-LATE"
    assert "good" not in d.evidence_ref.lower()
    assert "bad" not in d.evidence_ref.lower()


def test_post_start_description_and_ac_edit():
    """Description/AC edits after the issue enters in-progress are captured."""
    work_began = _at(5)
    changes = [
        _fc("S-1", _at(3), "description", event_id="pre"),
        _fc("S-1", _at(8), "description", event_id="desc"),
        _fc("S-1", _at(9), "acceptance_criteria", event_id="ac"),
        _fc("S-1", _at(10), "summary", event_id="other"),
    ]
    dims = IssueDimensions(project_key="PROJ", epic_key="EPIC-1")
    detections = detect_spec_changes(changes, work_began, dimensions=dims)

    assert len(detections) == 2
    assert [d.field for d in detections] == ["description", "acceptance_criteria"]
    assert detections[0].evidence_ref == "issue:S-1/field:description:desc:0"
    assert detections[1].evidence_ref == "issue:S-1/field:acceptance_criteria:ac:0"
    assert detections[0].work_began_at == work_began
    assert detections[0].project_key == "PROJ"
    assert detections[0].epic_key == "EPIC-1"

    assert detect_spec_changes(changes, None, dimensions=dims) == []


def test_empty_and_never_started_epics_are_zero():
    """No children / never-started children → empty result, no errors."""
    empty = measure_epic_scope("EPIC-EMPTY", [], {}, classify=CLASSIFY)
    assert empty.child_count == 0
    assert empty.epic_started_at is None
    assert empty.late_children == ()
    assert empty.late_child_count == 0

    idle_children = [
        EpicChild("C-1", "EPIC-IDLE", _at(1), project_key="PROJ"),
        EpicChild("C-2", "EPIC-IDLE", _at(20), project_key="PROJ"),
    ]
    idle_transitions = {
        "C-1": [_t("C-1", _at(2), None, "To Do", event_id="1")],
        "C-2": [],
    }
    idle = measure_epic_scope(
        "EPIC-IDLE", idle_children, idle_transitions, classify=CLASSIFY
    )
    assert idle.epic_started_at is None
    assert idle.late_child_count == 0
    assert detect_late_children(idle_children, None) == []


def test_aggregate_volume_and_timing_rollups():
    late = [
        LateChildDetection(
            epic_key="EPIC-1",
            external_key="L-1",
            child_created_at=_at(12, month=1),
            epic_started_at=_at(5, month=1),
            delay_seconds=7 * 86400,
            project_key="PROJ",
        ),
        LateChildDetection(
            epic_key="EPIC-2",
            external_key="L-2",
            child_created_at=_at(20, month=1),
            epic_started_at=_at(8, month=1),
            delay_seconds=12 * 86400,
            project_key="PROJ",
        ),
    ]
    specs = [
        SpecChangeDetection(
            external_key="S-1",
            field="description",
            changed_at=_at(15, month=1),
            work_began_at=_at(5, month=1),
            evidence_external_event_id="e1",
            evidence_event_index=0,
            project_key="PROJ",
            epic_key="EPIC-1",
        ),
    ]
    rows = aggregate_scope_signals(late, specs)
    by = {(p, d, k): (late_n, spec_n) for p, d, k, late_n, spec_n in rows}
    assert by[(date(2024, 1, 1), AggregateDimension.PROJECT, "PROJ")] == (2, 1)
    assert by[(date(2024, 1, 1), AggregateDimension.EPIC, "EPIC-1")] == (1, 1)
    assert by[(date(2024, 1, 1), AggregateDimension.EPIC, "EPIC-2")] == (1, 0)


def test_store_scope_change_idempotent_with_evidence(db_session, org_ready):
    org = org_ready
    other = Org(name="Other Scope Org")
    db_session.add(other)
    db_session.flush()

    with use_org(org.id):
        db_session.add_all(
            [
                Issue(
                    org_id=org.id,
                    external_key="C-1",
                    project_key="PROJ",
                    epic_key="EPIC-9",
                    source_created_at=_at(1),
                ),
                Issue(
                    org_id=org.id,
                    external_key="C-LATE",
                    project_key="PROJ",
                    epic_key="EPIC-9",
                    source_created_at=_at(20),
                ),
                Issue(
                    org_id=org.id,
                    external_key="S-EDIT",
                    project_key="PROJ",
                    epic_key="EPIC-9",
                    source_created_at=_at(2),
                ),
                # Epic with no children referenced as a key only — no Issue rows.
                Issue(
                    org_id=org.id,
                    external_key="ORPHAN",
                    project_key="PROJ",
                    epic_key=None,
                    source_created_at=_at(1),
                ),
            ]
        )
        for t in [
            _t("C-1", _at(10), "To Do", "In Progress", event_id="1"),
            _t("S-EDIT", _at(8), "To Do", "In Progress", event_id="2"),
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
        db_session.add(
            IssueFieldChange(
                org_id=org.id,
                external_key="S-EDIT",
                changed_at=_at(12),
                field="acceptance_criteria",
                external_event_id="ac1",
                event_index=0,
            )
        )
        db_session.commit()

        late_n, spec_n, agg_n = compute_and_store_scope_change_for_org(
            db_session, org.id, classify=CLASSIFY
        )
        db_session.commit()
        assert late_n == 1
        assert spec_n == 1
        assert agg_n >= 2

        late_events = list(db_session.scalars(select(LateChildEvent)).all())
        assert len(late_events) == 1
        late_id = late_events[0].id
        assert late_events[0].external_key == "C-LATE"
        assert late_events[0].evidence_ref == "epic:EPIC-9/late-child:C-LATE"
        # Epic start = earliest child in-progress (S-EDIT at day 8), not C-1.
        assert late_events[0].epic_started_at == _at(8)
        assert late_events[0].delay_seconds == int((_at(20) - _at(8)).total_seconds())

        spec_events = list(db_session.scalars(select(SpecChangeEvent)).all())
        assert len(spec_events) == 1
        spec_id = spec_events[0].id
        assert spec_events[0].evidence_ref == (
            "issue:S-EDIT/field:acceptance_criteria:ac1:0"
        )
        assert not hasattr(spec_events[0], "judgment")
        assert not hasattr(late_events[0], "is_bad")

        aggs = list(db_session.scalars(select(ScopeChangeAggregate)).all())
        by = {
            (r.dimension, r.dimension_key): (r.late_child_count, r.spec_change_count)
            for r in aggs
        }
        assert by[("project", "PROJ")] == (1, 1)
        assert by[("epic", "EPIC-9")] == (1, 1)

        late_n2, spec_n2, agg_n2 = compute_and_store_scope_change_for_org(
            db_session, org.id, classify=CLASSIFY
        )
        db_session.commit()
        assert (late_n2, spec_n2, agg_n2) == (late_n, spec_n, agg_n)
        assert next(iter(db_session.scalars(select(LateChildEvent)).all())).id == late_id
        assert next(iter(db_session.scalars(select(SpecChangeEvent)).all())).id == spec_id

    with use_org(other.id):
        assert db_session.scalars(select(LateChildEvent)).all() == []
        assert db_session.scalars(select(SpecChangeEvent)).all() == []
        assert db_session.scalars(select(ScopeChangeAggregate)).all() == []


def test_store_never_started_epic_creates_no_late_events(db_session, org_ready):
    org = org_ready
    with use_org(org.id):
        db_session.add_all(
            [
                Issue(
                    org_id=org.id,
                    external_key="IDLE-1",
                    project_key="PROJ",
                    epic_key="EPIC-IDLE",
                    source_created_at=_at(1),
                ),
                Issue(
                    org_id=org.id,
                    external_key="IDLE-2",
                    project_key="PROJ",
                    epic_key="EPIC-IDLE",
                    source_created_at=_at(30),
                ),
            ]
        )
        db_session.add(
            IssueTransition(
                org_id=org.id,
                external_key="IDLE-1",
                transitioned_at=_at(2),
                from_status=None,
                to_status="To Do",
                actor_id="u1",
                actor_display_name="Alice",
                external_event_id="1",
                event_index=0,
            )
        )
        db_session.commit()

        late_n, spec_n, agg_n = compute_and_store_scope_change_for_org(
            db_session, org.id, classify=CLASSIFY
        )
        db_session.commit()
        assert late_n == 0
        assert spec_n == 0
        assert agg_n == 0
        assert db_session.scalars(select(LateChildEvent)).all() == []
        assert db_session.scalars(select(SpecChangeEvent)).all() == []
        assert db_session.scalars(select(ScopeChangeAggregate)).all() == []
