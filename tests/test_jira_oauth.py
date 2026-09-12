"""Jira OAuth 3LO connection: callback store + refresh failure (issue #10).

Uses mocked Atlassian HTTP — no live developer-console app required.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.app import app
from throughline.api.auth import clear_jwks_cache
from throughline.config import settings
from throughline.connectors.jira import oauth as jira_oauth_http
from throughline.connectors.jira.crypto import decrypt_secret, encrypt_secret
from throughline.connectors.jira.oauth import AtlassianOAuthError, TokenResponse
from throughline.connectors.jira.service import (
    create_oauth_state,
    get_active_connection,
    get_valid_access_token,
)
from throughline.db.models import (
    JiraConnection,
    JiraConnectionStatus,
    Membership,
    MembershipRole,
    Org,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.tenancy import use_org

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-1"
FERNET_KEY = "dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="


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
    monkeypatch.setattr(settings, "clerk_bootstrap_org_name", "Dev Org")
    monkeypatch.setattr(settings, "atlassian_client_id", "test-client-id")
    monkeypatch.setattr(settings, "atlassian_client_secret", "test-client-secret")
    monkeypatch.setattr(
        settings,
        "atlassian_redirect_uri",
        "http://localhost:8000/connectors/jira/oauth/callback",
    )
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


@pytest.fixture
def org_ready(db_session):
    db_session.execute(delete(JiraConnection))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()

    org = Org(name="Jira Org")
    user = User(auth_subject="jira-admin-sub", email="admin@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    membership = Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
    db_session.add(membership)
    db_session.commit()
    return org, user


@pytest.fixture
def auth_header(org_ready, rsa_keypair):
    org, _ = org_ready
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="jira-admin-sub")
    return org, {"Authorization": f"Bearer {token}", "X-Org-Id": str(org.id)}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class _FakeHTTPResponse:
    def __init__(self, payload: object):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_authorize_url_includes_offline_access(client: TestClient, auth_header):
    _, headers = auth_header
    response = client.get("/admin/jira/oauth/authorize", headers=headers)
    assert response.status_code == 200
    url = response.json()["authorize_url"]
    assert "auth.atlassian.com/authorize" in url
    query = parse_qs(urlparse(url).query)
    scopes = query["scope"][0].split()
    assert "offline_access" in scopes
    assert query["client_id"] == ["test-client-id"]
    assert query["prompt"] == ["consent"]
    assert query["response_type"] == ["code"]


def test_oauth_callback_stores_encrypted_credentials(
    client: TestClient,
    db_session,
    auth_header,
    monkeypatch: pytest.MonkeyPatch,
):
    org, headers = auth_header
    state = create_oauth_state(org.id)

    def fake_urlopen(req, timeout=30):
        _ = timeout
        url = req.get_full_url()
        if url == jira_oauth_http.TOKEN_URL:
            return _FakeHTTPResponse(
                {
                    "access_token": "access-plain",
                    "refresh_token": "refresh-plain",
                    "expires_in": 3600,
                    "scope": "read:jira-work offline_access",
                }
            )
        if url == jira_oauth_http.ACCESSIBLE_RESOURCES_URL:
            return _FakeHTTPResponse(
                [
                    {
                        "id": "cloud-abc",
                        "url": "https://example.atlassian.net",
                        "name": "Example",
                        "scopes": ["read:jira-work"],
                    }
                ]
            )
        raise AssertionError(f"Unexpected URL {url}")

    monkeypatch.setattr(jira_oauth_http, "urlopen", fake_urlopen)

    response = client.get(
        "/connectors/jira/oauth/callback",
        params={"code": "auth-code", "state": state},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["cloud_id"] == "cloud-abc"
    assert "access_token" not in body
    assert "refresh_token" not in body

    with use_org(org.id):
        connection = get_active_connection(db_session)
        assert connection is not None
        assert connection.status == JiraConnectionStatus.CONNECTED
        assert connection.encrypted_access_token is not None
        assert connection.encrypted_refresh_token is not None
        assert connection.encrypted_access_token != b"access-plain"
        assert decrypt_secret(connection.encrypted_access_token) == "access-plain"
        assert decrypt_secret(connection.encrypted_refresh_token) == "refresh-plain"

    status_resp = client.get("/admin/jira/connection", headers=headers)
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["connected"] is True
    assert status_body["status"] == "connected"
    assert status_body["cloud_id"] == "cloud-abc"
    assert "access_token" not in status_body
    assert "refresh_token" not in status_body


def test_refresh_failure_sets_error_status(
    db_session,
    org_ready,
    monkeypatch: pytest.MonkeyPatch,
):
    org, _ = org_ready
    with use_org(org.id):
        connection = JiraConnection(
            org_id=org.id,
            cloud_id="cloud-abc",
            site_url="https://example.atlassian.net",
            site_name="Example",
            encrypted_access_token=encrypt_secret("old-access"),
            encrypted_refresh_token=encrypt_secret("bad-refresh"),
            access_token_expires_at=datetime.now(UTC) - timedelta(minutes=1),
            status=JiraConnectionStatus.CONNECTED,
            scopes="read:jira-work offline_access",
        )
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)

        def fail_refresh(_token: str):
            raise AtlassianOAuthError("Atlassian token request failed: invalid_grant")

        monkeypatch.setattr(
            "throughline.connectors.jira.service.refresh_access_token",
            fail_refresh,
        )

        with pytest.raises(AtlassianOAuthError):
            get_valid_access_token(db_session, connection)

        db_session.refresh(connection)
        assert connection.status == JiraConnectionStatus.ERROR
        assert connection.encrypted_access_token is None
        assert connection.encrypted_refresh_token is None
        assert "invalid_grant" in (connection.status_detail or "")


def test_error_status_visible_via_admin_api(
    client: TestClient,
    db_session,
    auth_header,
):
    org, headers = auth_header
    with use_org(org.id):
        connection = JiraConnection(
            org_id=org.id,
            status=JiraConnectionStatus.ERROR,
            status_detail="Atlassian token request failed: invalid_grant",
        )
        db_session.add(connection)
        db_session.commit()

    response = client.get("/admin/jira/connection", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["status"] == "error"
    assert "invalid_grant" in body["detail"]


def test_disconnect_leaves_disconnected_status(
    client: TestClient,
    db_session,
    auth_header,
):
    org, headers = auth_header
    with use_org(org.id):
        connection = JiraConnection(
            org_id=org.id,
            cloud_id="cloud-abc",
            encrypted_access_token=encrypt_secret("access"),
            encrypted_refresh_token=encrypt_secret("refresh"),
            access_token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=JiraConnectionStatus.CONNECTED,
        )
        db_session.add(connection)
        db_session.commit()

    assert client.get("/admin/jira/connection", headers=headers).json()["connected"] is True

    deleted = client.delete("/admin/jira/connection", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["connected"] is False
    assert deleted.json()["status"] == "disconnected"

    after = client.get("/admin/jira/connection", headers=headers)
    assert after.json()["connected"] is False
    assert after.json()["status"] == "disconnected"


def test_transparent_refresh_success(
    db_session,
    org_ready,
    monkeypatch: pytest.MonkeyPatch,
):
    org, _ = org_ready
    with use_org(org.id):
        connection = JiraConnection(
            org_id=org.id,
            cloud_id="cloud-abc",
            encrypted_access_token=encrypt_secret("old-access"),
            encrypted_refresh_token=encrypt_secret("old-refresh"),
            access_token_expires_at=datetime.now(UTC) + timedelta(seconds=30),
            status=JiraConnectionStatus.CONNECTED,
        )
        db_session.add(connection)
        db_session.commit()
        db_session.refresh(connection)

        def ok_refresh(token: str):
            assert token == "old-refresh"
            return TokenResponse(
                access_token="new-access",
                refresh_token="new-refresh",
                expires_in=3600,
                scope="read:jira-work offline_access",
            )

        monkeypatch.setattr(
            "throughline.connectors.jira.service.refresh_access_token",
            ok_refresh,
        )
        token = get_valid_access_token(db_session, connection)
        assert token == "new-access"
        db_session.refresh(connection)
        assert decrypt_secret(connection.encrypted_access_token) == "new-access"
        assert decrypt_secret(connection.encrypted_refresh_token) == "new-refresh"
        assert connection.status == JiraConnectionStatus.CONNECTED
