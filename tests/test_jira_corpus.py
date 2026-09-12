"""Public Jira corpus loader (issue #16) — fixture path + ToS gate."""

from __future__ import annotations

import socket
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from throughline.config import settings
from throughline.db.models import (
    Issue,
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
from throughline.ingest.corpus import (
    CorpusTosError,
    ensure_tos_allowed,
    load_corpus,
    load_tos_status,
)
from throughline.ingest.corpus.__main__ import main as corpus_main
from throughline.ingest.corpus.loader import CORPUS_ORG_NAME, CorpusLoadResult
from throughline.ingest.corpus.tos import tos_doc_path
from throughline.tenancy import use_org

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
def db_session(migrated_db: None):
    Session = get_session_factory()
    with Session() as session:
        yield session


def _wipe(db_session) -> None:
    for model in (
        IssueTransition,
        Issue,
        JiraStatusTransition,
        JiraIssue,
        SyncState,
        JiraFieldMapping,
        JiraFieldDefinition,
        JiraStatus,
        JiraIssueType,
        Project,
        JiraConnection,
        Membership,
        User,
        Org,
    ):
        try:
            db_session.execute(delete(model))
        except SQLAlchemyError:
            db_session.rollback()
            raise
    db_session.commit()


def test_tos_status_parses_allowed_markers():
    status = load_tos_status(root=REPO_ROOT)
    assert status.source == "apache_issues"
    assert status.status == "allowed"
    assert status.verified_date == "2026-09-12"
    ensure_tos_allowed(root=REPO_ROOT)


def test_tos_missing_doc_refuses(tmp_path: Path):
    with pytest.raises(CorpusTosError, match="missing"):
        load_tos_status(root=tmp_path)


def test_tos_disallowed_refuses(tmp_path: Path):
    docs = tmp_path / "_docs"
    docs.mkdir()
    (docs / "public-jira-corpus.md").write_text(
        "\n".join(
            [
                "corpus-tos-source: apache_issues",
                "corpus-tos-verified-date: 2026-09-12",
                "corpus-tos-status: disallowed",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusTosError, match="disallowed"):
        ensure_tos_allowed(root=tmp_path)


def test_fixture_corpus_loads_issues_and_transitions(db_session):
    _wipe(db_session)
    result = load_corpus(db_session, mode="fixture", root=REPO_ROOT)
    assert result.mode == "fixture"
    assert result.issue_count == 2
    assert result.transition_count >= 2
    assert result.tos_verified_date == "2026-09-12"

    with use_org(result.org_id):
        keys = sorted(db_session.scalars(select(JiraIssue.issue_key)).all())
        assert keys == ["CORPUS-1", "CORPUS-2"]
        transitions = list(db_session.scalars(select(JiraStatusTransition)).all())
        assert len(transitions) == 5
        org = db_session.scalar(select(Org).where(Org.name == CORPUS_ORG_NAME))
        assert org is not None


def test_loader_refuses_when_tos_disallowed(db_session, tmp_path: Path):
    _wipe(db_session)
    docs = tmp_path / "_docs"
    docs.mkdir()
    (docs / "public-jira-corpus.md").write_text(
        "\n".join(
            [
                "corpus-tos-source: apache_issues",
                "corpus-tos-verified-date: 2099-01-01",
                "corpus-tos-status: disallowed",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(CorpusTosError, match="disallowed"):
        load_corpus(db_session, mode="fixture", root=tmp_path)
    assert db_session.scalar(select(func.count()).select_from(JiraIssue)) == 0


def test_cli_prints_ok_on_fixture_load(capsys):
    fake = CorpusLoadResult(
        org_id=uuid.uuid4(),
        mode="fixture",
        issue_count=2,
        transition_count=5,
        tos_verified_date="2026-09-12",
    )

    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    with (
        patch("throughline.ingest.corpus.__main__.get_session_factory", return_value=_Session),
        patch("throughline.ingest.corpus.__main__.load_corpus", return_value=fake),
    ):
        code = corpus_main([])
    captured = capsys.readouterr()
    assert code == 0
    assert "ok mode=fixture" in captured.out
    assert "issues=2" in captured.out
    assert "transitions=5" in captured.out


def test_cli_exits_2_on_tos_error(capsys):
    class _Session:
        def __enter__(self):
            return object()

        def __exit__(self, *args):
            return False

    with (
        patch("throughline.ingest.corpus.__main__.get_session_factory", return_value=_Session),
        patch(
            "throughline.ingest.corpus.__main__.load_corpus",
            side_effect=CorpusTosError("marked disallowed"),
        ),
    ):
        code = corpus_main(["--remote"])
    captured = capsys.readouterr()
    assert code == 2
    assert "disallowed" in captured.err


def test_tos_doc_path_points_at_repo_note():
    path = tos_doc_path(root=REPO_ROOT)
    assert path == REPO_ROOT / "_docs" / "public-jira-corpus.md"
    assert path.is_file()
