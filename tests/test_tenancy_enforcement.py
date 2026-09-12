"""Tenancy enforcement layer (issue #7)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pytest
from alembic import command
from alembic.config import Config
from fastapi import Depends, FastAPI, Request, Response
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.deps import require_current_org
from throughline.db.models import Membership, MembershipRole, Org, User
from throughline.db.session import (
    get_engine,
    get_session_factory,
    sqlalchemy_database_url,
)
from throughline.tenancy import (
    TenantContextError,
    get_current_org_id,
    require_current_org_id,
    reset_current_org_id,
    set_current_org_id,
    skip_tenant_enforcement,
    tenant_select,
    use_org,
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
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose)")
    command.upgrade(_alembic_config(), "head")


@pytest.fixture
def db_session(migrated_db: None):
    Session = get_session_factory()
    with Session() as session:
        yield session


@pytest.fixture
def two_org_memberships(db_session):
    """Seed Org A and Org B each with a membership; soft-delete one A membership."""
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()

    org_a = Org(name="Org A")
    org_b = Org(name="Org B")
    user_a = User(email="a@example.com", display_name="User A")
    user_b = User(email="b@example.com", display_name="User B")
    user_a_soft = User(email="a-gone@example.com", display_name="Soft A")
    db_session.add_all([org_a, org_b, user_a, user_b, user_a_soft])
    db_session.flush()

    mem_a = Membership(org_id=org_a.id, user_id=user_a.id, role=MembershipRole.ADMIN)
    mem_a_soft = Membership(
        org_id=org_a.id,
        user_id=user_a_soft.id,
        role=MembershipRole.VIEWER,
        deleted_at=datetime.now(UTC),
    )
    mem_b = Membership(org_id=org_b.id, user_id=user_b.id, role=MembershipRole.PM)
    db_session.add_all([mem_a, mem_a_soft, mem_b])
    db_session.commit()

    for obj in (org_a, org_b, mem_a, mem_a_soft, mem_b):
        db_session.refresh(obj)

    return {
        "org_a": org_a,
        "org_b": org_b,
        "mem_a": mem_a,
        "mem_a_soft": mem_a_soft,
        "mem_b": mem_b,
    }


def test_org_listing_not_forced_through_org_filter(two_org_memberships, db_session) -> None:
    """Org has no org_id filter — listing works without a current org."""
    assert get_current_org_id() is None
    orgs = list(
        db_session.scalars(select(Org).where(Org.deleted_at.is_(None)).order_by(Org.name)).all()
    )
    assert {o.name for o in orgs} == {"Org A", "Org B"}


def test_cross_tenant_query_excludes_other_org(two_org_memberships, db_session) -> None:
    org_a = two_org_memberships["org_a"]
    mem_a = two_org_memberships["mem_a"]
    mem_b = two_org_memberships["mem_b"]

    with use_org(org_a.id):
        rows = list(db_session.scalars(select(Membership)).all())
        helper_rows = list(db_session.scalars(tenant_select(Membership)).all())

    ids = {row.id for row in rows}
    assert mem_a.id in ids
    assert mem_b.id not in ids
    assert {row.id for row in helper_rows} == {mem_a.id}
    assert all(row.org_id == org_a.id for row in rows)


def test_soft_deleted_tenant_rows_excluded_by_default(two_org_memberships, db_session) -> None:
    org_a = two_org_memberships["org_a"]
    mem_a = two_org_memberships["mem_a"]
    mem_a_soft = two_org_memberships["mem_a_soft"]

    with use_org(org_a.id):
        rows = list(db_session.scalars(select(Membership)).all())
        helper_rows = list(db_session.scalars(tenant_select(Membership)).all())

    ids = {row.id for row in rows}
    assert mem_a.id in ids
    assert mem_a_soft.id not in ids
    assert {row.id for row in helper_rows} == {mem_a.id}


def test_tenant_query_without_org_fails_loudly(two_org_memberships, db_session) -> None:
    assert get_current_org_id() is None
    with pytest.raises(TenantContextError, match="No current organization"):
        db_session.scalars(select(Membership)).all()


def test_deliberate_bypass_cleared_context_fails_loudly(two_org_memberships, db_session) -> None:
    """Clearing/forging away org context must raise — not leak cross-tenant rows."""
    org_a = two_org_memberships["org_a"]
    with use_org(org_a.id):
        assert require_current_org_id() == org_a.id

    assert get_current_org_id() is None
    with pytest.raises(TenantContextError):
        list(db_session.scalars(select(Membership)).all())


def test_deliberate_unscoped_path_without_skip_fails(two_org_memberships, db_session) -> None:
    """Raw tenant select without current org (helper required) fails loudly."""
    with pytest.raises(TenantContextError):
        db_session.scalars(tenant_select(Membership)).all()


def test_platform_admin_skip_is_explicit(two_org_memberships, db_session) -> None:
    """Documented escape hatch returns all active memberships when opted in."""
    assert get_current_org_id() is None
    stmt = skip_tenant_enforcement(
        select(Membership).where(Membership.deleted_at.is_(None))
    )
    rows = list(db_session.scalars(stmt).all())
    assert {row.id for row in rows} == {
        two_org_memberships["mem_a"].id,
        two_org_memberships["mem_b"].id,
    }


def test_tenant_select_rejects_non_tenant_model() -> None:
    with pytest.raises(TypeError, match="not tenant-scoped"):
        tenant_select(Org)  # type: ignore[arg-type]


def test_fastapi_dependency_requires_org_with_middleware() -> None:
    """Middleware binds the contextvar; dependency requires/returns current org."""
    probe = FastAPI()

    @probe.middleware("http")
    async def tenant_context_middleware(request: Request, call_next) -> Response:
        raw = request.headers.get("x-org-id")
        org_id = uuid.UUID(raw) if raw else None
        token = set_current_org_id(org_id)
        try:
            return await call_next(request)
        finally:
            reset_current_org_id(token)

    @probe.get("/whoami")
    def whoami(
        org_id: Annotated[uuid.UUID, Depends(require_current_org)],
    ) -> dict[str, str]:
        assert get_current_org_id() == org_id
        return {"org_id": str(org_id)}

    org = uuid.uuid4()
    client = TestClient(probe)
    missing = client.get("/whoami")
    assert missing.status_code == 400

    ok = client.get("/whoami", headers={"X-Org-Id": str(org)})
    assert ok.status_code == 200
    assert ok.json()["org_id"] == str(org)
    assert get_current_org_id() is None


def test_middleware_binds_and_clears_org_context() -> None:
    """Request-scoped middleware sets contextvar from X-Org-Id and resets after."""
    probe = FastAPI()

    @probe.middleware("http")
    async def tenant_context_middleware(request: Request, call_next) -> Response:
        raw = request.headers.get("x-org-id")
        org_id = uuid.UUID(raw) if raw else None
        token = set_current_org_id(org_id)
        try:
            return await call_next(request)
        finally:
            reset_current_org_id(token)

    seen: dict[str, uuid.UUID | None] = {}

    @probe.get("/ctx")
    def ctx() -> dict[str, str | None]:
        current = get_current_org_id()
        seen["during"] = current
        return {"org_id": str(current) if current else None}

    org = uuid.uuid4()
    client = TestClient(probe)
    response = client.get("/ctx", headers={"X-Org-Id": str(org)})
    assert response.status_code == 200
    assert response.json()["org_id"] == str(org)
    assert seen["during"] == org
    assert get_current_org_id() is None
