"""Resumable Jira changelog status-transition import (issue #14).

Uses recorded changelog fixtures / mocks — no live Jira.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.app import app
from throughline.api.auth import clear_jwks_cache
from throughline.config import settings
from throughline.connectors.jira.client import JiraAPIError, JiraClient
from throughline.connectors.jira.crypto import encrypt_secret
from throughline.connectors.jira.import_changelog import (
    CONNECTOR_JIRA,
    SYNC_KEY_CHANGELOG,
    run_changelog_import,
)
from throughline.db.models import (
    JiraConnection,
    JiraConnectionStatus,
    JiraIssue,
    JiraStatusTransition,
    Membership,
    MembershipRole,
    Org,
    SyncRunStatus,
    SyncState,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.tenancy import use_org
from throughline.workers.settings import WorkerSettings, import_jira_changelog_job

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-changelog"
FERNET_KEY = "dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="

_CHANGELOG_PATH = re.compile(r"/issue/(?P<key>[^/]+)/changelog$")


def _load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _query(url: str) -> dict[str, list[str]]:
    return parse_qs(urlsplit(url).query)


@pytest.fixture(scope="module")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture(scope="module")
def jwks_static_json(rsa_keypair) -> str:
    _, public_key = rsa_keypair
    public_numbers = public_key.public_numbers()

    def _b64url_uint(value: int) -> str:
        length = (value.bit_length() + 7) // 8
        return jwt.utils.base64url_encode(value.to_bytes(length, "big")).decode("ascii")

    return json.dumps(
        {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": TEST_KID,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url_uint(public_numbers.n),
                    "e": _b64url_uint(public_numbers.e),
                }
            ]
        }
    )


@pytest.fixture(autouse=True)
def configure_settings(jwks_static_json, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clerk_issuer", TEST_ISSUER)
    monkeypatch.setattr(settings, "clerk_jwks_static_json", jwks_static_json)
    monkeypatch.setattr(settings, "clerk_jwks_url", "")
    monkeypatch.setattr(settings, "clerk_audience", "")
    monkeypatch.setattr(settings, "clerk_bootstrap_org_id", None)
    monkeypatch.setattr(settings, "credentials_encryption_key", FERNET_KEY)
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def mint_token(private_key, *, sub: str) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": TEST_ISSUER,
        "iat": now - 10,
        "exp": now + 3600,
        "email": "admin@example.com",
        "name": "Admin",
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": TEST_KID})


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_database_url())
    return cfg


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except (OSError, SQLAlchemyError):
        return False


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
    db_session.execute(delete(JiraStatusTransition))
    db_session.execute(delete(JiraIssue))
    db_session.execute(delete(SyncState))
    db_session.execute(delete(JiraConnection))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Changelog Org")
    user = User(auth_subject="changelog-admin-sub", email="admin@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    membership = Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
    db_session.add(membership)
    db_session.commit()
    return org, user


def _connect_jira(db_session, org, *, cloud_id: str = "cloud-import") -> JiraConnection:
    connection = JiraConnection(
        org_id=org.id,
        cloud_id=cloud_id,
        site_url="https://example.atlassian.net",
        site_name="Example",
        encrypted_access_token=encrypt_secret("fixture-access"),
        encrypted_refresh_token=encrypt_secret("fixture-refresh"),
        access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        status=JiraConnectionStatus.CONNECTED,
    )
    db_session.add(connection)
    db_session.commit()
    return connection


def _seed_issues(db_session, org) -> None:
    rows = [
        ("10001", "ALPHA-1"),
        ("10002", "ALPHA-2"),
        ("10003", "ALPHA-3"),
        ("10004", "ALPHA-4"),
    ]
    for external_id, key in rows:
        db_session.add(
            JiraIssue(
                org_id=org.id,
                external_id=external_id,
                issue_key=key,
                project_key="ALPHA",
                summary=f"Seed {key}",
            )
        )
    db_session.commit()


def _changelog_opener(*, fail_on_key: str | None = None):
    """Serve per-issue changelog fixtures; optionally fail when hitting a key."""
    single = {
        "ALPHA-1": _load("changelog_alpha_1.json"),
        "ALPHA-2": _load("changelog_alpha_2.json"),
        "ALPHA-3": _load("changelog_alpha_3.json"),
    }
    paged = {
        "ALPHA-4": {
            0: _load("changelog_alpha_4_page1.json"),
            1: _load("changelog_alpha_4_page2.json"),
        }
    }
    calls: list[tuple[str, int]] = []

    def opener(req, timeout=60):
        _ = timeout
        path = urlsplit(req.full_url).path
        match = _CHANGELOG_PATH.search(path)
        assert match, path
        key = match.group("key")
        start = int(_query(req.full_url).get("startAt", ["0"])[0])
        calls.append((key, start))
        if fail_on_key is not None and key == fail_on_key and start == 0:
            raise JiraAPIError("simulated changelog failure", retryable=True)
        if key in single:
            assert start == 0, (key, start)
            return _FakeResponse(single[key])
        if key in paged:
            pages = paged[key]
            if start not in pages:
                raise AssertionError(f"Unexpected startAt={start} for {key}")
            return _FakeResponse(pages[start])
        raise AssertionError(f"Unexpected issue key={key}")

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


@pytest.fixture
def auth_header(org_ready, rsa_keypair):
    org, _ = org_ready
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="changelog-admin-sub")
    return org, {"Authorization": f"Bearer {token}", "X-Org-Id": str(org.id)}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_worker_registers_changelog_job() -> None:
    assert import_jira_changelog_job in WorkerSettings.functions


def test_full_changelog_matches_fixture_transitions(db_session, org_ready):
    org, _ = org_ready
    connection = _connect_jira(db_session, org)
    _seed_issues(db_session, org)
    opener = _changelog_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )

    with use_org(org.id):
        result = run_changelog_import(db_session, jira, org.id, page_size=50)

    assert result.completed is True
    assert result.imported_count == 4
    assert result.issues_processed == 4
    # ALPHA-1: 2, ALPHA-2: 4 (incl. reopen), ALPHA-3: 0, ALPHA-4: 2
    assert result.transitions_stored == 8
    assert opener.calls == [
        ("ALPHA-1", 0),
        ("ALPHA-2", 0),
        ("ALPHA-3", 0),
        ("ALPHA-4", 0),
        ("ALPHA-4", 1),
    ]

    with use_org(org.id):
        rows = list(
            db_session.scalars(
                select(JiraStatusTransition).order_by(
                    JiraStatusTransition.issue_key,
                    JiraStatusTransition.transitioned_at,
                    JiraStatusTransition.history_id,
                )
            ).all()
        )
        sync = db_session.scalar(
            select(SyncState).where(
                SyncState.connector == CONNECTOR_JIRA,
                SyncState.sync_key == SYNC_KEY_CHANGELOG,
            )
        )
        db_session.refresh(connection)
        flagged = db_session.scalar(
            select(func.count())
            .select_from(JiraIssue)
            .where(JiraIssue.changelog_imported_at.is_not(None))
        )

    assert flagged == 4
    assert sync is not None
    assert sync.status == SyncRunStatus.COMPLETED
    assert sync.cursor == "ALPHA-4"
    assert sync.imported_count == 4
    assert connection.changelog_status == SyncRunStatus.COMPLETED.value
    assert connection.changelog_imported_count == 4
    assert connection.changelog_cursor == "ALPHA-4"
    assert connection.changelog_total_estimate == 4

    assert len(rows) == 8

    alpha1 = [r for r in rows if r.issue_key == "ALPHA-1"]
    assert [(r.from_status_name, r.to_status_name, r.actor_display_name) for r in alpha1] == [
        ("Open", "In Progress", "Alice Admin"),
        ("In Progress", "Done", "Bob Builder"),
    ]
    assert alpha1[0].transitioned_at == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert alpha1[0].actor_account_id == "acct-alice"
    assert alpha1[0].from_status_id == "1"
    assert alpha1[0].to_status_id == "3"
    assert alpha1[1].transitioned_at == datetime(2024, 1, 2, 15, 30, tzinfo=UTC)

    alpha2 = [r for r in rows if r.issue_key == "ALPHA-2"]
    assert [(r.from_status_name, r.to_status_name) for r in alpha2] == [
        ("Open", "In Progress"),
        ("In Progress", "Done"),
        ("Done", "Open"),
        ("Open", "In Progress"),
    ]
    assert alpha2[2].actor_display_name == "Dave Reviewer"
    assert alpha2[2].history_id == "10203"

    assert [r for r in rows if r.issue_key == "ALPHA-3"] == []

    alpha4 = [r for r in rows if r.issue_key == "ALPHA-4"]
    assert [(r.from_status_name, r.to_status_name, r.history_id) for r in alpha4] == [
        ("Open", "In Progress", "10401"),
        ("In Progress", "Done", "10402"),
    ]


def test_resume_after_interruption_skips_completed_issues(db_session, org_ready):
    org, _ = org_ready
    connection = _connect_jira(db_session, org)
    _seed_issues(db_session, org)

    fail_opener = _changelog_opener(fail_on_key="ALPHA-3")
    jira_fail = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=fail_opener,
        sleeper=lambda _d: None,
        max_attempts=1,
    )
    with use_org(org.id), pytest.raises(JiraAPIError, match="simulated changelog failure"):
        run_changelog_import(db_session, jira_fail, org.id, page_size=50)

    assert fail_opener.calls == [("ALPHA-1", 0), ("ALPHA-2", 0), ("ALPHA-3", 0)]

    with use_org(org.id):
        keys_done = sorted(
            db_session.scalars(
                select(JiraIssue.issue_key).where(
                    JiraIssue.changelog_imported_at.is_not(None)
                )
            ).all()
        )
        sync = db_session.scalar(
            select(SyncState).where(SyncState.sync_key == SYNC_KEY_CHANGELOG)
        )
        db_session.refresh(connection)

    assert keys_done == ["ALPHA-1", "ALPHA-2"]
    assert sync is not None
    assert sync.status == SyncRunStatus.FAILED
    assert sync.cursor == "ALPHA-2"
    assert connection.changelog_status == SyncRunStatus.FAILED.value
    assert connection.changelog_cursor == "ALPHA-2"

    resume_opener = _changelog_opener()
    jira_resume = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=resume_opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        result = run_changelog_import(db_session, jira_resume, org.id, page_size=50)

    assert result.completed is True
    # Resume must not re-fetch ALPHA-1 / ALPHA-2.
    assert resume_opener.calls == [("ALPHA-3", 0), ("ALPHA-4", 0), ("ALPHA-4", 1)]
    assert result.issues_processed == 2

    with use_org(org.id):
        count = db_session.scalar(select(func.count()).select_from(JiraStatusTransition))
        sync = db_session.scalar(
            select(SyncState).where(SyncState.sync_key == SYNC_KEY_CHANGELOG)
        )
        db_session.refresh(connection)

    assert count == 8
    assert sync is not None
    assert sync.status == SyncRunStatus.COMPLETED
    assert connection.changelog_status == SyncRunStatus.COMPLETED.value


def test_max_issues_soft_stop_then_resume(db_session, org_ready):
    org, _ = org_ready
    _connect_jira(db_session, org)
    _seed_issues(db_session, org)
    opener = _changelog_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        partial = run_changelog_import(
            db_session, jira, org.id, page_size=50, max_issues=2
        )
    assert partial.completed is False
    assert partial.cursor == "ALPHA-2"
    assert opener.calls == [("ALPHA-1", 0), ("ALPHA-2", 0)]

    with use_org(org.id):
        result = run_changelog_import(db_session, jira, org.id, page_size=50)
    assert result.completed is True
    assert opener.calls == [
        ("ALPHA-1", 0),
        ("ALPHA-2", 0),
        ("ALPHA-3", 0),
        ("ALPHA-4", 0),
        ("ALPHA-4", 1),
    ]


def test_rerun_does_not_duplicate_transitions(db_session, org_ready):
    org, _ = org_ready
    _connect_jira(db_session, org)
    _seed_issues(db_session, org)
    opener = _changelog_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        first = run_changelog_import(db_session, jira, org.id, page_size=50)
        first_ids = sorted(
            db_session.scalars(select(JiraStatusTransition.id)).all(),
            key=str,
        )

    assert first.transitions_stored == 8

    rerun_opener = _changelog_opener()
    jira2 = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=rerun_opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        second = run_changelog_import(db_session, jira2, org.id, page_size=50)
        count = db_session.scalar(select(func.count()).select_from(JiraStatusTransition))
        second_ids = sorted(
            db_session.scalars(select(JiraStatusTransition.id)).all(),
            key=str,
        )

    # Fully imported issues are skipped — no changelog HTTP calls, no new rows.
    assert second.issues_processed == 0
    assert second.transitions_stored == 0
    assert rerun_opener.calls == []
    assert count == 8
    assert second_ids == first_ids


def test_admin_changelog_progress_requires_auth(client: TestClient):
    assert client.get("/admin/jira/changelog").status_code == 401


def test_admin_changelog_progress_readable(
    client: TestClient, db_session, org_ready, auth_header
):
    org, headers = auth_header
    _connect_jira(db_session, org)
    _seed_issues(db_session, org)
    opener = _changelog_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        run_changelog_import(db_session, jira, org.id, page_size=50)

    response = client.get("/admin/jira/changelog", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == SyncRunStatus.COMPLETED.value
    assert body["imported_count"] == 4
    assert body["cursor"] == "ALPHA-4"
    assert body["total_estimate"] == 4


def test_admin_enqueue_requires_connection(client: TestClient, org_ready, auth_header):
    _, headers = auth_header
    response = client.post("/admin/jira/changelog", headers=headers)
    assert response.status_code == 409
