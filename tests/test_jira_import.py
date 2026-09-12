"""Resumable Jira issue history import via JQL (issue #13).

Uses recorded search fixtures / mocks — no live Jira.
"""

from __future__ import annotations

import json
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
from throughline.connectors.jira.import_history import (
    CONNECTOR_JIRA,
    SYNC_KEY_ISSUE_HISTORY,
    run_issue_history_import,
)
from throughline.db.models import (
    JiraConnection,
    JiraConnectionStatus,
    JiraIssue,
    Membership,
    MembershipRole,
    Org,
    SyncRunStatus,
    SyncState,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.tenancy import use_org
from throughline.workers.settings import WorkerSettings, import_jira_issue_history_job

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-import"
FERNET_KEY = "dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="


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
    org = Org(name="Import Org")
    user = User(auth_subject="import-admin-sub", email="admin@example.com")
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


def _paged_opener(*, fail_at_start: int | None = None):
    """Serve import_search_page{1,2}.json; optionally fail after a given startAt."""
    pages = {
        0: _load("import_search_page1.json"),
        2: _load("import_search_page2.json"),
    }
    calls: list[int] = []

    def opener(req, timeout=60):
        _ = timeout
        path = urlsplit(req.full_url).path
        assert path.endswith("/search"), path
        start = int(_query(req.full_url)["startAt"][0])
        calls.append(start)
        if fail_at_start is not None and start == fail_at_start:
            raise JiraAPIError("simulated mid-run failure", retryable=True)
        if start not in pages:
            raise AssertionError(f"Unexpected startAt={start}")
        return _FakeResponse(pages[start])

    opener.calls = calls  # type: ignore[attr-defined]
    return opener


@pytest.fixture
def auth_header(org_ready, rsa_keypair):
    org, _ = org_ready
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="import-admin-sub")
    return org, {"Authorization": f"Bearer {token}", "X-Org-Id": str(org.id)}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_worker_registers_import_job() -> None:
    assert import_jira_issue_history_job in WorkerSettings.functions


def test_full_import_persists_issues_and_progress(db_session, org_ready):
    org, _ = org_ready
    connection = _connect_jira(db_session, org)
    opener = _paged_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )

    with use_org(org.id):
        result = run_issue_history_import(db_session, jira, org.id, page_size=2)

    assert result.completed is True
    assert result.imported_count == 4
    assert result.pages_processed == 2
    assert opener.calls == [0, 2]

    with use_org(org.id):
        keys = sorted(db_session.scalars(select(JiraIssue.issue_key)).all())
        sync = db_session.scalar(
            select(SyncState).where(
                SyncState.connector == CONNECTOR_JIRA,
                SyncState.sync_key == SYNC_KEY_ISSUE_HISTORY,
            )
        )
        db_session.refresh(connection)

    assert keys == ["ALPHA-1", "ALPHA-2", "ALPHA-3", "ALPHA-4"]
    assert sync is not None
    assert sync.status == SyncRunStatus.COMPLETED
    assert sync.cursor == "4"
    assert sync.imported_count == 4
    assert connection.import_status == SyncRunStatus.COMPLETED.value
    assert connection.import_imported_count == 4
    assert connection.import_cursor == "4"
    assert connection.import_total_estimate == 4


def test_resume_after_interruption_does_not_restart_from_zero(db_session, org_ready):
    org, _ = org_ready
    connection = _connect_jira(db_session, org)

    # Run 1: succeed page 1, fail when requesting page 2.
    fail_opener = _paged_opener(fail_at_start=2)
    jira_fail = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=fail_opener,
        sleeper=lambda _d: None,
        max_attempts=1,
    )
    with use_org(org.id), pytest.raises(JiraAPIError, match="simulated mid-run failure"):
        run_issue_history_import(db_session, jira_fail, org.id, page_size=2)

    assert fail_opener.calls == [0, 2]

    with use_org(org.id):
        keys_after_fail = sorted(db_session.scalars(select(JiraIssue.issue_key)).all())
        sync = db_session.scalar(select(SyncState))
        db_session.refresh(connection)

    assert keys_after_fail == ["ALPHA-1", "ALPHA-2"]
    assert sync is not None
    assert sync.status == SyncRunStatus.FAILED
    assert sync.cursor == "2"
    assert connection.import_status == SyncRunStatus.FAILED.value
    assert connection.import_cursor == "2"
    assert connection.import_imported_count == 2

    # Run 2 (worker restart): resume from cursor startAt=2 only.
    resume_opener = _paged_opener()
    jira_resume = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=resume_opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        result = run_issue_history_import(db_session, jira_resume, org.id, page_size=2)

    assert result.completed is True
    assert resume_opener.calls == [2]
    assert result.imported_count == 4

    with use_org(org.id):
        count = db_session.scalar(select(func.count()).select_from(JiraIssue))
        sync = db_session.scalar(select(SyncState))
        db_session.refresh(connection)

    assert count == 4
    assert sync is not None
    assert sync.status == SyncRunStatus.COMPLETED
    assert connection.import_status == SyncRunStatus.COMPLETED.value


def test_max_pages_soft_stop_then_resume(db_session, org_ready):
    org, _ = org_ready
    _connect_jira(db_session, org)
    opener = _paged_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        partial = run_issue_history_import(
            db_session, jira, org.id, page_size=2, max_pages=1
        )
    assert partial.completed is False
    assert partial.cursor == "2"
    assert opener.calls == [0]

    with use_org(org.id):
        result = run_issue_history_import(db_session, jira, org.id, page_size=2)
    assert result.completed is True
    assert opener.calls == [0, 2]


def test_reimport_is_idempotent_for_issue_keys(db_session, org_ready):
    org, _ = org_ready
    _connect_jira(db_session, org)
    opener = _paged_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        run_issue_history_import(db_session, jira, org.id, page_size=2)
        first_id = db_session.scalar(
            select(JiraIssue.id).where(JiraIssue.issue_key == "ALPHA-4")
        )

    # Mutate page2 summary so the second run updates in place.
    page2 = _load("import_search_page2.json")
    page2["issues"][1]["fields"]["summary"] = "Fourth issue — reimported"
    pages = {0: _load("import_search_page1.json"), 2: page2}

    def opener2(req, timeout=60):
        _ = timeout
        start = int(_query(req.full_url)["startAt"][0])
        return _FakeResponse(pages[start])

    jira2 = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener2,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        run_issue_history_import(db_session, jira2, org.id, page_size=2)
        count = db_session.scalar(select(func.count()).select_from(JiraIssue))
        row = db_session.scalar(select(JiraIssue).where(JiraIssue.issue_key == "ALPHA-4"))

    assert count == 4
    assert row is not None
    assert row.id == first_id
    assert row.summary == "Fourth issue — reimported"


def test_admin_import_progress_requires_auth(client: TestClient):
    assert client.get("/admin/jira/import").status_code == 401


def test_admin_import_progress_readable(
    client: TestClient, db_session, org_ready, auth_header
):
    org, headers = auth_header
    _connect_jira(db_session, org)
    opener = _paged_opener()
    jira = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-import/rest/api/3",
        access_token="tok",
        opener=opener,
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        run_issue_history_import(db_session, jira, org.id, page_size=2)

    response = client.get("/admin/jira/import", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == SyncRunStatus.COMPLETED.value
    assert body["imported_count"] == 4
    assert body["cursor"] == "4"
    assert body["total_estimate"] == 4


def test_admin_enqueue_requires_connection(client: TestClient, org_ready, auth_header):
    _, headers = auth_header
    response = client.post("/admin/jira/import", headers=headers, json={})
    assert response.status_code == 409
