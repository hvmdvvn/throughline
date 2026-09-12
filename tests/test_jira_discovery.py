"""Jira project/field discovery + per-org field mapping (issue #12).

Uses recorded fixtures for two differently configured sites — no live Jira.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
from throughline.connectors.jira.client import JiraClient
from throughline.connectors.jira.crypto import encrypt_secret
from throughline.connectors.jira.discovery import (
    resolve_mapped_field_id,
    run_discovery,
)
from throughline.db.models import (
    JiraConnection,
    JiraConnectionStatus,
    JiraFieldConcept,
    JiraFieldDefinition,
    JiraFieldMapping,
    JiraIssueType,
    JiraStatus,
    Membership,
    MembershipRole,
    Org,
    Project,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.tenancy import use_org

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-1"
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


def _site_opener(site: str):
    """Route JiraClient GETs to discovery fixtures for site a or b."""
    projects = _load(f"discovery_site_{site}_projects.json")
    issue_types = _load(f"discovery_site_{site}_issuetypes.json")
    statuses = _load(f"discovery_site_{site}_statuses.json")
    fields = _load(f"discovery_site_{site}_fields.json")

    def opener(req, timeout=60):
        _ = timeout
        path = urlsplit(req.full_url).path
        if path.endswith("/project/search"):
            return _FakeResponse(projects)
        if path.endswith("/issuetype"):
            return _FakeResponse(issue_types)
        if path.endswith("/status"):
            return _FakeResponse(statuses)
        if path.endswith("/field"):
            return _FakeResponse(fields)
        raise AssertionError(f"Unexpected Jira path: {path}")

    return opener


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
    db_session.execute(delete(JiraFieldMapping))
    db_session.execute(delete(JiraFieldDefinition))
    db_session.execute(delete(JiraStatus))
    db_session.execute(delete(JiraIssueType))
    db_session.execute(delete(Project))
    db_session.execute(delete(JiraConnection))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Discovery Org")
    user = User(auth_subject="discovery-admin-sub", email="admin@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    membership = Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
    db_session.add(membership)
    db_session.commit()
    return org, user


def _connect_jira(db_session, org, *, cloud_id: str = "cloud-fixture") -> JiraConnection:
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


@pytest.fixture
def auth_header(org_ready, rsa_keypair):
    org, _ = org_ready
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="discovery-admin-sub")
    return org, {"Authorization": f"Bearer {token}", "X-Org-Id": str(org.id)}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_mapping_lookup_differs_across_fixture_sites(db_session, org_ready):
    org, _ = org_ready
    with use_org(org.id):
        client_a = JiraClient.from_base_url(
            base_url="https://api.atlassian.com/ex/jira/cloud-a/rest/api/3",
            access_token="tok",
            opener=_site_opener("a"),
            sleeper=lambda _d: None,
        )
        result_a = run_discovery(db_session, client_a, org.id)
        assert result_a.projects == 2
        assert result_a.fields >= 3
        assert resolve_mapped_field_id(db_session, JiraFieldConcept.ACCEPTANCE_CRITERIA) == (
            "customfield_10010"
        )
        assert (
            resolve_mapped_field_id(db_session, JiraFieldConcept.STORY_POINTS)
            == "customfield_10016"
        )

    _wipe(db_session)
    org_b = Org(name="Site B Org")
    db_session.add(org_b)
    db_session.commit()

    with use_org(org_b.id):
        client_b = JiraClient.from_base_url(
            base_url="https://api.atlassian.com/ex/jira/cloud-b/rest/api/3",
            access_token="tok",
            opener=_site_opener("b"),
            sleeper=lambda _d: None,
        )
        result_b = run_discovery(db_session, client_b, org_b.id)
        assert result_b.projects == 1
        # Differently configured field set → different mapped ids.
        assert resolve_mapped_field_id(db_session, JiraFieldConcept.ACCEPTANCE_CRITERIA) == (
            "customfield_20001"
        )
        assert (
            resolve_mapped_field_id(db_session, JiraFieldConcept.STORY_POINTS)
            == "customfield_20002"
        )
        assert resolve_mapped_field_id(db_session, JiraFieldConcept.STORY_POINTS) != (
            "customfield_10016"
        )


def test_rediscovery_updates_without_duplicating_projects(db_session, org_ready):
    org, _ = org_ready
    client = JiraClient.from_base_url(
        base_url="https://api.atlassian.com/ex/jira/cloud-a/rest/api/3",
        access_token="tok",
        opener=_site_opener("a"),
        sleeper=lambda _d: None,
    )
    with use_org(org.id):
        run_discovery(db_session, client, org.id)
        first_ids = {
            p.external_id: p.id
            for p in db_session.scalars(select(Project)).all()
        }
        assert len(first_ids) == 2

        # Rename a project in the "API" response and re-run.
        projects_payload = _load("discovery_site_a_projects.json")
        projects_payload["values"][0]["name"] = "Alpha Renamed"

        def opener_renamed(req, timeout=60):
            _ = timeout
            path = urlsplit(req.full_url).path
            if path.endswith("/project/search"):
                return _FakeResponse(projects_payload)
            return _site_opener("a")(req, timeout=timeout)

        client2 = JiraClient.from_base_url(
            base_url="https://api.atlassian.com/ex/jira/cloud-a/rest/api/3",
            access_token="tok",
            opener=opener_renamed,
            sleeper=lambda _d: None,
        )
        run_discovery(db_session, client2, org.id)
        rows = list(db_session.scalars(select(Project)).all())
        assert len(rows) == 2
        by_ext = {p.external_id: p for p in rows}
        assert by_ext["10000"].id == first_ids["10000"]
        assert by_ext["10000"].name == "Alpha Renamed"
        assert by_ext["10001"].id == first_ids["10001"]

        count = db_session.scalar(select(func.count()).select_from(Project))
        assert count == 2


def test_lookup_uses_mapping_table_not_hardcoded_ids(db_session, org_ready):
    org, _ = org_ready
    with use_org(org.id):
        db_session.add(
            JiraFieldDefinition(
                org_id=org.id,
                field_id="customfield_99999",
                name="Whatever Label",
                custom=True,
            )
        )
        db_session.add(
            JiraFieldMapping(
                org_id=org.id,
                concept=JiraFieldConcept.STORY_POINTS,
                jira_field_id="customfield_99999",
            )
        )
        db_session.commit()
        # Application path resolves via mapping row only.
        assert (
            resolve_mapped_field_id(db_session, JiraFieldConcept.STORY_POINTS)
            == "customfield_99999"
        )


def test_admin_api_lists_projects_and_mappings(
    client: TestClient, auth_header, db_session, org_ready, monkeypatch
):
    org, headers = auth_header
    _connect_jira(db_session, org)

    def fake_build(db, *, opener=None):
        _ = opener
        return JiraClient.from_base_url(
            base_url="https://api.atlassian.com/ex/jira/cloud-a/rest/api/3",
            access_token="tok",
            opener=_site_opener("a"),
            sleeper=lambda _d: None,
        )

    monkeypatch.setattr(
        "throughline.api.routes.jira_discovery.build_client_for_connection",
        fake_build,
    )

    discovered = client.post("/admin/jira/discovery", headers=headers)
    assert discovered.status_code == 200, discovered.text
    body = discovered.json()
    assert body["projects"] == 2
    assert body["issue_types"] == 2
    assert body["statuses"] == 3
    assert body["fields"] >= 3
    assert body["mappings_seeded"] >= 1

    projects = client.get("/admin/jira/projects", headers=headers)
    assert projects.status_code == 200
    keys = {p["key"] for p in projects.json()}
    assert keys == {"ALPHA", "BETA"}
    assert all(p["org_id"] == str(org.id) for p in projects.json())

    mappings = client.get("/admin/jira/field-mappings", headers=headers)
    assert mappings.status_code == 200
    by_concept = {m["concept"]: m["jira_field_id"] for m in mappings.json()}
    assert by_concept["acceptance_criteria"] == "customfield_10010"
    assert by_concept["story_points"] == "customfield_10016"

    # Manual override stays data-driven.
    put = client.put(
        "/admin/jira/field-mappings",
        headers=headers,
        json={
            "mappings": [
                {"concept": "story_points", "jira_field_id": "customfield_10020"},
            ]
        },
    )
    assert put.status_code == 200, put.text
    by_concept = {m["concept"]: m["jira_field_id"] for m in put.json()}
    assert by_concept["story_points"] == "customfield_10020"

    with use_org(org.id):
        assert (
            resolve_mapped_field_id(db_session, JiraFieldConcept.STORY_POINTS)
            == "customfield_10020"
        )


def test_discovery_requires_connection(client: TestClient, auth_header):
    _, headers = auth_header
    response = client.post("/admin/jira/discovery", headers=headers)
    assert response.status_code == 409
