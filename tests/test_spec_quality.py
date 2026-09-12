"""Spec quality indicators: AC/description proxies (issue #20).

Comment-traffic indicators depend on comment import (#69) and are asserted
as explicitly unavailable until that lands.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics.spec_quality import (
    COMMENT_IMPORT_DEPENDENCY_ISSUE,
    COMMENT_TRAFFIC_STATUS_UNAVAILABLE,
    SHORT_DESCRIPTION_MAX_CHARS,
    AcCoverage,
    DescriptionLengthBucket,
    SpecQualityDimension,
    ac_coverage_for_issue,
    aggregate_spec_quality,
    comment_traffic_indicator,
    compute_and_store_spec_quality_for_org,
    description_length_bucket,
    detect_spec_quality_for_issue,
)
from throughline.config import settings
from throughline.db.models import (
    Issue,
    JiraFieldConcept,
    JiraFieldMapping,
    Membership,
    MembershipRole,
    Org,
    SpecQualityAggregate,
    SpecQualityIssueIndicator,
    User,
)
from throughline.db.session import get_session_factory, sqlalchemy_database_url
from throughline.domain.issues import CanonicalIssue
from throughline.tenancy import use_org

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    db_session.execute(delete(SpecQualityAggregate))
    db_session.execute(delete(SpecQualityIssueIndicator))
    db_session.execute(delete(JiraFieldMapping))
    db_session.execute(delete(Issue))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Spec Quality Org")
    user = User(auth_subject="spec-quality-admin", email="sq@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org


def _issue(
    key: str,
    *,
    project: str | None = "PROJ",
    team: str | None = "Platform",
    ac: str | None = None,
    description: str | None = None,
    when: datetime | None = None,
) -> CanonicalIssue:
    at = when or datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
    return CanonicalIssue(
        external_key=key,
        project_key=project,
        summary=f"Summary {key}",
        status="To Do",
        issue_type="Story",
        acceptance_criteria=ac,
        story_points=None,
        created_at=at,
        updated_at=at,
        epic_key=None,
        description=description,
        team_key=team,
    )


def test_empty_ac_and_mapping_unavailable_are_distinct():
    assert (
        ac_coverage_for_issue(None, ac_field_mapped=False)
        == AcCoverage.MAPPING_UNAVAILABLE
    )
    assert (
        ac_coverage_for_issue("", ac_field_mapped=False) == AcCoverage.MAPPING_UNAVAILABLE
    )
    assert ac_coverage_for_issue(None, ac_field_mapped=True) == AcCoverage.MISSING_OR_EMPTY
    assert ac_coverage_for_issue("   ", ac_field_mapped=True) == AcCoverage.MISSING_OR_EMPTY
    assert (
        ac_coverage_for_issue("Given When Then", ac_field_mapped=True) == AcCoverage.PRESENT
    )


def test_short_vs_long_description_buckets():
    assert description_length_bucket(0) == DescriptionLengthBucket.EMPTY
    assert (
        description_length_bucket(SHORT_DESCRIPTION_MAX_CHARS)
        == DescriptionLengthBucket.SHORT
    )
    assert (
        description_length_bucket(SHORT_DESCRIPTION_MAX_CHARS + 1)
        == DescriptionLengthBucket.LONG
    )

    short = detect_spec_quality_for_issue(
        _issue("S-1", ac="ok", description="x" * 10),
        ac_field_mapped=True,
    )
    long = detect_spec_quality_for_issue(
        _issue("S-2", ac="ok", description="y" * (SHORT_DESCRIPTION_MAX_CHARS + 5)),
        ac_field_mapped=True,
    )
    empty = detect_spec_quality_for_issue(
        _issue("S-3", ac="ok", description=None),
        ac_field_mapped=True,
    )
    assert short.description_length_bucket == DescriptionLengthBucket.SHORT
    assert short.description_length_chars == 10
    assert long.description_length_bucket == DescriptionLengthBucket.LONG
    assert empty.description_length_bucket == DescriptionLengthBucket.EMPTY
    assert empty.description_length_chars == 0
    # Naming is indicator/proxy — no graded score field on the detection.
    assert not hasattr(short, "quality_score")
    assert short.evidence_ref == "issue:S-1/spec-quality-indicators"


def test_comment_traffic_unavailable_until_issue_69():
    """Comment-traffic AC is blocked on #69; assert explicit unavailable status.

    When comments become available (#69), with/without traffic must diverge.
    """
    assert COMMENT_IMPORT_DEPENDENCY_ISSUE == 69

    status, count = comment_traffic_indicator(comments_available=False)
    assert status == COMMENT_TRAFFIC_STATUS_UNAVAILABLE
    assert count is None

    # Future path once #69 lands: with vs without clarification-like traffic.
    none_status, none_count = comment_traffic_indicator(
        comments_available=True,
        clarification_like_count=0,
    )
    present_status, present_count = comment_traffic_indicator(
        comments_available=True,
        clarification_like_count=3,
    )
    assert none_status == "none" and none_count == 0
    assert present_status == "present" and present_count == 3

    blocked = detect_spec_quality_for_issue(
        _issue("C-1", ac="ok", description="desc"),
        ac_field_mapped=True,
        comments_available=False,
    )
    assert blocked.comment_traffic_status == COMMENT_TRAFFIC_STATUS_UNAVAILABLE
    assert blocked.comment_traffic_count is None


def test_aggregate_by_project_and_team_with_evidence_keys():
    detections = [
        detect_spec_quality_for_issue(
            _issue("A-1", project="ALPHA", team="Platform", ac=None, description="short"),
            ac_field_mapped=True,
        ),
        detect_spec_quality_for_issue(
            _issue(
                "A-2",
                project="ALPHA",
                team="Platform",
                ac="Given AC",
                description="x" * (SHORT_DESCRIPTION_MAX_CHARS + 2),
            ),
            ac_field_mapped=True,
        ),
        detect_spec_quality_for_issue(
            _issue("B-1", project="BETA", team="Growth", ac="  ", description=None),
            ac_field_mapped=True,
        ),
    ]
    rows = aggregate_spec_quality(detections)
    by_dim = {(r.dimension, r.dimension_key): r for r in rows}

    alpha = by_dim[(SpecQualityDimension.PROJECT, "ALPHA")]
    assert alpha.issue_count == 2
    assert alpha.ac_missing_or_empty_count == 1
    assert alpha.ac_present_count == 1
    assert alpha.description_short_count == 1
    assert alpha.description_long_count == 1
    assert alpha.evidence_issue_keys == ("A-1", "A-2")
    assert alpha.comment_traffic_unavailable is True

    platform = by_dim[(SpecQualityDimension.TEAM, "Platform")]
    assert platform.issue_count == 2
    assert platform.evidence_issue_keys == ("A-1", "A-2")

    growth = by_dim[(SpecQualityDimension.TEAM, "Growth")]
    assert growth.issue_count == 1
    assert growth.ac_missing_or_empty_count == 1
    assert growth.description_empty_count == 1
    assert growth.evidence_issue_keys == ("B-1",)

    # No person-dimension keys.
    allowed = {SpecQualityDimension.PROJECT, SpecQualityDimension.TEAM}
    assert all(r.dimension in allowed for r in rows)


def test_store_spec_quality_idempotent_and_mapping_unavailable(db_session, org_ready):
    org = org_ready

    with use_org(org.id):
        # No AC mapping → mapping_unavailable, not silent missing zeros.
        db_session.add(
            Issue(
                org_id=org.id,
                external_key="U-1",
                project_key="PROJ",
                team_key="Platform",
                summary="Unmapped AC",
                description="hello",
                acceptance_criteria=None,
                source_created_at=datetime(2024, 3, 1, tzinfo=UTC),
                source_updated_at=datetime(2024, 3, 1, tzinfo=UTC),
            )
        )
        db_session.commit()

        n, agg_n = compute_and_store_spec_quality_for_org(db_session, org.id)
        db_session.commit()
        assert n == 1
        assert agg_n >= 2  # project + team

        indicators = list(db_session.scalars(select(SpecQualityIssueIndicator)).all())
        assert len(indicators) == 1
        row = indicators[0]
        assert row.ac_coverage == AcCoverage.MAPPING_UNAVAILABLE.value
        assert row.description_length_bucket == DescriptionLengthBucket.SHORT.value
        assert row.comment_traffic_status == COMMENT_TRAFFIC_STATUS_UNAVAILABLE
        assert row.comment_traffic_count is None
        assert row.evidence_ref == "issue:U-1/spec-quality-indicators"
        assert "score" not in row.ac_coverage
        indicator_id = row.id

        aggs = list(db_session.scalars(select(SpecQualityAggregate)).all())
        assert aggs
        assert all(a.comment_traffic_unavailable for a in aggs)
        assert all(a.dimension in {"project", "team"} for a in aggs)
        assert any(a.evidence_issue_keys == ["U-1"] for a in aggs)

        # Add mapping + empty AC issue; recompute is idempotent on U-1.
        db_session.add(
            JiraFieldMapping(
                org_id=org.id,
                concept=JiraFieldConcept.ACCEPTANCE_CRITERIA,
                jira_field_id="customfield_10010",
            )
        )
        db_session.add(
            Issue(
                org_id=org.id,
                external_key="U-2",
                project_key="PROJ",
                team_key="Platform",
                summary="Empty AC",
                description="x" * (SHORT_DESCRIPTION_MAX_CHARS + 3),
                acceptance_criteria="",
                source_created_at=datetime(2024, 3, 2, tzinfo=UTC),
                source_updated_at=datetime(2024, 3, 2, tzinfo=UTC),
            )
        )
        db_session.commit()

        n2, _ = compute_and_store_spec_quality_for_org(db_session, org.id)
        db_session.commit()
        assert n2 == 2

        by_key = {
            r.external_key: r
            for r in db_session.scalars(select(SpecQualityIssueIndicator)).all()
        }
        assert by_key["U-1"].id == indicator_id
        # Mapping now exists; U-1 still has null AC → missing_or_empty.
        assert by_key["U-1"].ac_coverage == AcCoverage.MISSING_OR_EMPTY.value
        assert by_key["U-2"].ac_coverage == AcCoverage.MISSING_OR_EMPTY.value
        assert by_key["U-2"].description_length_bucket == DescriptionLengthBucket.LONG.value

        # Soft-delete an issue → indicator removed on recompute.
        issue_u2 = db_session.scalar(select(Issue).where(Issue.external_key == "U-2"))
        assert issue_u2 is not None
        issue_u2.deleted_at = datetime.now(UTC)
        db_session.commit()
        compute_and_store_spec_quality_for_org(db_session, org.id)
        db_session.commit()
        active = list(
            db_session.scalars(
                select(SpecQualityIssueIndicator).where(
                    SpecQualityIssueIndicator.deleted_at.is_(None)
                )
            ).all()
        )
        assert [r.external_key for r in active] == ["U-1"]
