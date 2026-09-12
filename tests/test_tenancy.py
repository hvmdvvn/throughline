"""Tenancy models and authenticated admin list endpoints (issue #6)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.app import app
from throughline.config import settings
from throughline.db.models import Membership, MembershipRole, Org, User
from throughline.db.session import (
    get_engine,
    get_session_factory,
    sqlalchemy_database_url,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


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
    """Apply Alembic migrations when Postgres is available; otherwise skip."""
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose)")
    command.upgrade(_alembic_config(), "head")


@pytest.fixture
def db_session(migrated_db: None):
    Session = get_session_factory()
    with Session() as session:
        yield session


@pytest.fixture
def seeded_tenancy(db_session):
    """Insert one org, user, membership; soft-delete a second org for filter checks."""
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()

    org = Org(name="Acme")
    soft_deleted_org = Org(name="Gone Co", deleted_at=datetime.now(UTC))
    user = User(
        auth_subject="clerk_user_abc",
        email="pm@acme.example",
        display_name="Pat Manager",
    )
    db_session.add_all([org, soft_deleted_org, user])
    db_session.flush()

    membership = Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
    db_session.add(membership)
    db_session.commit()

    db_session.refresh(org)
    db_session.refresh(user)
    db_session.refresh(membership)
    return {"org": org, "user": user, "membership": membership}


@pytest.fixture
def admin_client() -> TestClient:
    return TestClient(app)


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.admin_api_key}"}


def test_org_user_membership_relationships(seeded_tenancy, db_session) -> None:
    org = seeded_tenancy["org"]
    user = seeded_tenancy["user"]
    membership = seeded_tenancy["membership"]

    loaded = db_session.scalar(select(Membership).where(Membership.id == membership.id))
    assert loaded is not None
    assert loaded.org_id == org.id
    assert loaded.user_id == user.id
    assert loaded.role == MembershipRole.ADMIN
    assert loaded.org.name == "Acme"
    assert loaded.user.email == "pm@acme.example"
    assert hasattr(loaded, "deleted_at")
    assert loaded.created_at is not None
    assert org.deleted_at is None
    assert "org_id" not in Org.__table__.c
    assert "org_id" in Membership.__table__.c
    assert "deleted_at" in Org.__table__.c
    assert "deleted_at" in User.__table__.c


def test_list_orgs_requires_auth(admin_client: TestClient, seeded_tenancy) -> None:
    response = admin_client.get("/admin/orgs")
    assert response.status_code == 401


def test_list_orgs_happy_path(admin_client: TestClient, seeded_tenancy) -> None:
    response = admin_client.get("/admin/orgs", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Acme"
    assert body[0]["id"] == str(seeded_tenancy["org"].id)
    assert "created_at" in body[0]
    assert "updated_at" in body[0]
    assert "deleted_at" not in body[0]


def test_list_users_happy_path(admin_client: TestClient, seeded_tenancy) -> None:
    response = admin_client.get("/admin/users", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["email"] == "pm@acme.example"
    assert body[0]["auth_subject"] == "clerk_user_abc"
    assert body[0]["display_name"] == "Pat Manager"


def test_list_memberships_happy_path(admin_client: TestClient, seeded_tenancy) -> None:
    response = admin_client.get("/admin/memberships", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["role"] == "admin"
    assert body[0]["org_id"] == str(seeded_tenancy["org"].id)
    assert body[0]["user_id"] == str(seeded_tenancy["user"].id)


def test_list_users_rejects_bad_token(admin_client: TestClient, seeded_tenancy) -> None:
    response = admin_client.get(
        "/admin/users",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_no_django_admin_routes(admin_client: TestClient) -> None:
    """Guardrail: this issue must not introduce Django admin."""
    assert not any(getattr(route, "path", "").startswith("/django") for route in app.routes)
    response = admin_client.get("/admin/")
    # FastAPI returns 404/405 for missing collection index — never a Django admin site.
    assert response.status_code in {404, 405}
    assert "django" not in response.text.lower()
