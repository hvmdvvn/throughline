"""Signal validation study (issue #26) — fixture path against ≥2 ToS sources."""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from throughline.analytics.signal_validation import (
    DEFAULT_STUDY_SOURCES,
    run_signal_validation_study,
    study_to_dict,
    write_study_artifact,
)
from throughline.analytics.signal_validation.__main__ import main as study_main
from throughline.config import settings
from throughline.db.models import (
    DiagnosticReport,
    Issue,
    IssueFieldChange,
    IssueTransition,
    JiraConnection,
    JiraFieldDefinition,
    JiraFieldMapping,
    JiraIssue,
    JiraIssueType,
    JiraStatus,
    JiraStatusTransition,
    Membership,
    Org,
    Project,
    SyncState,
    User,
)
from throughline.db.session import get_session_factory, sqlalchemy_database_url
from throughline.ingest.corpus.tos import JENKINS_SOURCE_ID, SOURCE_ID

REPO_ROOT = Path(__file__).resolve().parents[1]
FERNET_KEY = "dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="


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


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setattr(settings, "credentials_encryption_key", FERNET_KEY)


@pytest.fixture(scope="module")
def migrated_db() -> None:
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose)")
    command.upgrade(_alembic_config(), "head")


@pytest.fixture
def db_session(migrated_db):
    Session = get_session_factory()
    with Session() as session:
        yield session


def _wipe(session) -> None:
    for model in (
        DiagnosticReport,
        IssueFieldChange,
        IssueTransition,
        Issue,
        JiraStatusTransition,
        JiraIssue,
        SyncState,
        JiraFieldMapping,
        JiraFieldDefinition,
        JiraIssueType,
        JiraStatus,
        Project,
        JiraConnection,
        Membership,
        User,
        Org,
    ):
        try:
            session.execute(delete(model))
        except SQLAlchemyError:
            session.rollback()
            raise
    session.commit()


def test_fixture_study_runs_two_sources_and_writes_artifact(db_session, tmp_path: Path):
    _wipe(db_session)
    study = run_signal_validation_study(
        db_session,
        sources=DEFAULT_STUDY_SOURCES,
        mode="fixture",
        root=REPO_ROOT,
    )
    assert len(study.sources) == 2
    assert {s.source for s in study.sources} == {SOURCE_ID, JENKINS_SOURCE_ID}
    assert all(s.tos_status == "allowed" for s in study.sources)
    assert all(s.issue_count >= 2 for s in study.sources)
    assert all(s.report_status in {"success", "partial"} for s in study.sources)
    assert set(study.metric_keys)

    artifact = write_study_artifact(study, path=tmp_path / "metrics.json")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert len(payload["sources"]) == 2
    assert "metric_deltas" in payload
    assert payload["notes"]


def test_study_requires_two_sources(db_session):
    _wipe(db_session)
    with pytest.raises(ValueError, match="at least two"):
        run_signal_validation_study(
            db_session,
            sources=(SOURCE_ID,),
            mode="fixture",
            root=REPO_ROOT,
        )


def test_cli_fixture_study(capsys, tmp_path: Path):
    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    from throughline.analytics.signal_validation import (
        SignalValidationStudy,
        SourceStudyResult,
    )

    src_a = SourceStudyResult(
        source=SOURCE_ID,
        mode="fixture",
        org_id=str(uuid.uuid4()),
        org_name="A",
        issue_count=2,
        transition_count=5,
        tos_verified_date="2026-09-12",
        tos_status="allowed",
        report_id=str(uuid.uuid4()),
        report_version=1,
        report_status="success",
        range_start="2000-01-01",
        range_end="2030-12-31",
        metrics={},
        generation_detail={},
        error_message=None,
    )
    src_b = SourceStudyResult(
        source=JENKINS_SOURCE_ID,
        mode="fixture",
        org_id=str(uuid.uuid4()),
        org_name="B",
        issue_count=3,
        transition_count=10,
        tos_verified_date="2026-09-13",
        tos_status="allowed",
        report_id=str(uuid.uuid4()),
        report_version=1,
        report_status="success",
        range_start="2000-01-01",
        range_end="2030-12-31",
        metrics={},
        generation_detail={},
        error_message=None,
    )
    study = SignalValidationStudy(
        sources=(src_a, src_b),
        metric_keys=("reopen.event_count",),
        metric_deltas={
            "reopen.event_count": {
                "by_source": {SOURCE_ID: 1, JENKINS_SOURCE_ID: 2},
                "abs_delta": 1,
                "varies": True,
            }
        },
        notes=("ok",),
    )

    with (
        patch(
            "throughline.analytics.signal_validation.__main__.get_session_factory",
            return_value=_Session,
        ),
        patch(
            "throughline.analytics.signal_validation.__main__.run_signal_validation_study",
            return_value=study,
        ),
    ):
        code = study_main(
            ["--fixture", "--artifact", str(tmp_path / "out.json")]
        )
    captured = capsys.readouterr()
    assert code == 0
    assert "ok mode=fixture" in captured.out
    assert (tmp_path / "out.json").is_file()
    assert study_to_dict(study)["sources"][0]["source"] == SOURCE_ID
