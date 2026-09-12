"""Clerk JWT auth: verification, first-login mapping, org-from-membership (issue #8)."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

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
from throughline.db.models import Membership, MembershipRole, Org, User
from throughline.db.session import (
    get_engine,
    get_session_factory,
    sqlalchemy_database_url,
)
from throughline.tenancy import skip_tenant_enforcement

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-1"


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

    jwks = {
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
    return json.dumps(jwks)


@pytest.fixture(autouse=True)
def configure_clerk(rsa_keypair, jwks_static_json, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clerk_issuer", TEST_ISSUER)
    monkeypatch.setattr(settings, "clerk_jwks_static_json", jwks_static_json)
    monkeypatch.setattr(settings, "clerk_jwks_url", "")
    monkeypatch.setattr(settings, "clerk_audience", "")
    monkeypatch.setattr(settings, "clerk_bootstrap_org_id", None)
    monkeypatch.setattr(settings, "clerk_bootstrap_org_name", "Dev Org")
    clear_jwks_cache()
    yield
    clear_jwks_cache()


def mint_token(
    private_key,
    *,
    sub: str,
    email: str | None = "user@example.com",
    name: str | None = "Test User",
    expired: bool = False,
    issuer: str = TEST_ISSUER,
) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": issuer,
        "iat": now - 10,
        "exp": now - 60 if expired else now + 3600,
    }
    if email is not None:
        payload["email"] = email
    if name is not None:
        payload["name"] = name
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
def clean_identity(db_session):
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_missing_token_returns_401(client: TestClient, clean_identity) -> None:
    response = client.get("/me")
    assert response.status_code == 401


def test_invalid_token_returns_401(client: TestClient, clean_identity, rsa_keypair) -> None:
    private_key, _ = rsa_keypair
    # Wrong issuer → invalid
    token = mint_token(private_key, sub="user_invalid", issuer="https://evil.example")
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401

    expired = mint_token(private_key, sub="user_expired", expired=True)
    response = client.get("/me", headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401

    response = client.get("/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401


def test_first_login_creates_user_and_membership(
    client: TestClient, db_session, clean_identity, rsa_keypair
) -> None:
    private_key, _ = rsa_keypair
    sub = "clerk_user_first"
    token = mint_token(private_key, sub=sub, email="first@example.com", name="First User")

    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["auth_subject"] == sub
    assert body["email"] == "first@example.com"
    assert body["display_name"] == "First User"
    assert body["role"] == "admin"
    assert body["org_id"]

    users = list(db_session.scalars(select(User).where(User.auth_subject == sub)).all())
    assert len(users) == 1
    orgs = list(db_session.scalars(select(Org).where(Org.name == "Dev Org")).all())
    assert len(orgs) == 1
    memberships = list(
        db_session.scalars(
            skip_tenant_enforcement(
                select(Membership).where(Membership.user_id == users[0].id)
            )
        ).all()
    )
    assert len(memberships) == 1
    assert memberships[0].role == MembershipRole.ADMIN
    assert body["org_id"] == str(orgs[0].id)


def test_repeat_login_idempotent(
    client: TestClient, db_session, clean_identity, rsa_keypair
) -> None:
    private_key, _ = rsa_keypair
    sub = "clerk_user_repeat"
    token = mint_token(private_key, sub=sub, email="repeat@example.com")

    first = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200
    second = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert second.status_code == 200
    assert first.json()["user_id"] == second.json()["user_id"]
    assert first.json()["membership_id"] == second.json()["membership_id"]

    count = db_session.scalar(
        select(func.count()).select_from(User).where(User.auth_subject == sub)
    )
    assert count == 1
    user_id = uuid.UUID(first.json()["user_id"])
    mem_count = db_session.scalar(
        skip_tenant_enforcement(
            select(func.count()).select_from(Membership).where(Membership.user_id == user_id)
        )
    )
    assert mem_count == 1


def test_org_context_from_membership_without_header(
    client: TestClient, clean_identity, rsa_keypair
) -> None:
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="clerk_user_org_ctx")
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["org_id"]


def test_org_context_rejects_foreign_x_org_id(
    client: TestClient, db_session, clean_identity, rsa_keypair
) -> None:
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="clerk_user_foreign_org")
    first = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert first.status_code == 200

    foreign = Org(name="Other Org")
    db_session.add(foreign)
    db_session.commit()
    db_session.refresh(foreign)

    response = client.get(
        "/me",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Org-Id": str(foreign.id),
        },
    )
    assert response.status_code == 403


def test_bootstrap_org_id_setting(
    client: TestClient, db_session, clean_identity, rsa_keypair, monkeypatch: pytest.MonkeyPatch
) -> None:
    org = Org(name="Seeded Bootstrap")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    monkeypatch.setattr(settings, "clerk_bootstrap_org_id", org.id)

    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="clerk_user_bootstrap_id")
    response = client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["org_id"] == str(org.id)


def test_admin_routes_require_clerk_jwt(
    client: TestClient, db_session, clean_identity, rsa_keypair
) -> None:
    assert client.get("/admin/orgs").status_code == 401
    assert client.get("/admin/orgs", headers={"Authorization": "Bearer wrong"}).status_code == 401

    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="clerk_user_admin_list")
    # First login creates bootstrap identity; admin list should succeed.
    response = client.get("/admin/orgs", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert any(row["name"] == "Dev Org" for row in response.json())


def test_no_django_auth(client: TestClient) -> None:
    assert not any(getattr(route, "path", "").startswith("/django") for route in app.routes)
