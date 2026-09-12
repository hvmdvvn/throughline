"""Diagnostic report API: list, detail, evidence pagination, tenancy (issue #23)."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

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
from throughline.db.models import (
    DiagnosticReport,
    DiagnosticReportStatus,
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
RANGE_START = date(2024, 6, 1)
RANGE_END = date(2024, 6, 30)
METRIC_KEY = "cycle_time.completed_issue_count"


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
def configure_clerk(jwks_static_json, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clerk_issuer", TEST_ISSUER)
    monkeypatch.setattr(settings, "clerk_jwks_static_json", jwks_static_json)
    monkeypatch.setattr(settings, "clerk_jwks_url", "")
    monkeypatch.setattr(settings, "clerk_audience", "")
    monkeypatch.setattr(settings, "clerk_bootstrap_org_id", None)
    monkeypatch.setattr(settings, "clerk_bootstrap_org_name", "Dev Org")
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
        "email": f"{sub}@example.com",
        "name": "Report User",
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
    db_session.execute(delete(DiagnosticReport))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


def _seed_org(
    db_session,
    *,
    org_name: str,
    auth_subject: str,
) -> tuple[Org, User]:
    org = Org(name=org_name)
    user = User(auth_subject=auth_subject, email=f"{auth_subject}@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(
        Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN)
    )
    db_session.commit()
    return org, user


def _make_report(
    db_session,
    org: Org,
    *,
    version: int = 1,
    status: str = DiagnosticReportStatus.SUCCESS.value,
    evidence_refs: list[str] | None = None,
    deleted_at: datetime | None = None,
) -> DiagnosticReport:
    refs = evidence_refs if evidence_refs is not None else [
        f"issue:DEMO-{i}/cycle-time" for i in range(1, 6)
    ]
    with use_org(org.id):
        report = DiagnosticReport(
            org_id=org.id,
            range_start=RANGE_START,
            range_end=RANGE_END,
            version=version,
            status=status,
            metrics={
                METRIC_KEY: {"value": len(refs), "evidence_refs": refs},
                "reopen.event_count": {"value": 0, "evidence_refs": []},
            },
            generation_detail={"cycle_time": {"ok": True}},
            generated_at=datetime.now(UTC),
            error_message=None,
            deleted_at=deleted_at,
        )
        db_session.add(report)
        db_session.flush()
        report_id = report.id
        db_session.commit()
        # Soft-deleted rows are filtered by tenancy; skip refresh for those.
        if deleted_at is None:
            db_session.refresh(report)
        else:
            report.id = report_id
        return report


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org, user = _seed_org(db_session, org_name="Report Org", auth_subject="report-user")
    return org, user


@pytest.fixture
def auth_header(org_ready, rsa_keypair):
    org, _ = org_ready
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub="report-user")
    return org, {"Authorization": f"Bearer {token}", "X-Org-Id": str(org.id)}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_list_reports_for_current_org(client: TestClient, db_session, auth_header):
    org, headers = auth_header
    live = _make_report(db_session, org, version=2)
    _make_report(db_session, org, version=1)
    _make_report(
        db_session,
        org,
        version=3,
        deleted_at=datetime.now(UTC),
    )

    response = client.get("/reports", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    ids = {row["id"] for row in body}
    assert str(live.id) in ids
    assert all(row["org_id"] == str(org.id) for row in body)
    assert "metrics" not in body[0]


def test_get_report_metrics_payload(client: TestClient, db_session, auth_header):
    org, headers = auth_header
    report = _make_report(db_session, org)

    response = client.get(f"/reports/{report.id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(report.id)
    assert body["status"] == DiagnosticReportStatus.SUCCESS.value
    assert METRIC_KEY in body["metrics"]
    assert body["metrics"][METRIC_KEY]["value"] == 5
    assert body["metrics"][METRIC_KEY]["evidence_refs"] == [
        f"issue:DEMO-{i}/cycle-time" for i in range(1, 6)
    ]
    assert body["generation_detail"]["cycle_time"]["ok"] is True


def test_evidence_pagination(client: TestClient, db_session, auth_header):
    org, headers = auth_header
    refs = [f"issue:DEMO-{i}/cycle-time" for i in range(1, 12)]
    report = _make_report(db_session, org, evidence_refs=refs)

    page1 = client.get(
        f"/reports/{report.id}/metrics/{METRIC_KEY}/evidence",
        headers=headers,
        params={"page": 1, "limit": 5},
    )
    assert page1.status_code == 200
    body1 = page1.json()
    assert body1["total"] == 11
    assert body1["page"] == 1
    assert body1["limit"] == 5
    assert body1["has_more"] is True
    assert body1["items"] == refs[:5]
    assert body1["value"] == 11

    page2 = client.get(
        f"/reports/{report.id}/metrics/{METRIC_KEY}/evidence",
        headers=headers,
        params={"page": 2, "limit": 5},
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert body2["items"] == refs[5:10]
    assert body2["has_more"] is True

    page3 = client.get(
        f"/reports/{report.id}/metrics/{METRIC_KEY}/evidence",
        headers=headers,
        params={"page": 3, "limit": 5},
    )
    assert page3.status_code == 200
    body3 = page3.json()
    assert body3["items"] == refs[10:]
    assert body3["has_more"] is False


def test_missing_report_and_metric_errors(client: TestClient, db_session, auth_header):
    org, headers = auth_header
    report = _make_report(db_session, org)

    missing = client.get(f"/reports/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404

    bad_metric = client.get(
        f"/reports/{report.id}/metrics/not.a.real.metric/evidence",
        headers=headers,
    )
    assert bad_metric.status_code == 404

    soft = _make_report(db_session, org, version=9, deleted_at=datetime.now(UTC))
    deleted = client.get(f"/reports/{soft.id}", headers=headers)
    assert deleted.status_code == 404


def test_unauthorized_and_cross_tenant_denied(
    client: TestClient,
    db_session,
    auth_header,
    rsa_keypair,
):
    org_a, headers_a = auth_header
    report_a = _make_report(db_session, org_a)

    assert client.get("/reports").status_code == 401
    assert client.get(f"/reports/{report_a.id}").status_code == 401

    org_b, _ = _seed_org(db_session, org_name="Other Org", auth_subject="other-user")
    report_b = _make_report(db_session, org_b)

    # Authenticated as org A cannot read org B's report (tenant filter → 404).
    cross_get = client.get(f"/reports/{report_b.id}", headers=headers_a)
    assert cross_get.status_code == 404

    list_a = client.get("/reports", headers=headers_a)
    assert list_a.status_code == 200
    assert all(row["id"] != str(report_b.id) for row in list_a.json())

    # Forging X-Org-Id for an org the user does not belong to → 403.
    forged = {
        "Authorization": headers_a["Authorization"],
        "X-Org-Id": str(org_b.id),
    }
    assert client.get("/reports", headers=forged).status_code == 403
    assert client.get(f"/reports/{report_b.id}", headers=forged).status_code == 403

    # Org B member can see their own report, not A's.
    private_key, _ = rsa_keypair
    headers_b = {
        "Authorization": f"Bearer {mint_token(private_key, sub='other-user')}",
        "X-Org-Id": str(org_b.id),
    }
    assert client.get(f"/reports/{report_b.id}", headers=headers_b).status_code == 200
    assert client.get(f"/reports/{report_a.id}", headers=headers_b).status_code == 404


def test_evidence_limit_validation(client: TestClient, db_session, auth_header):
    org, headers = auth_header
    report = _make_report(db_session, org)

    too_large = client.get(
        f"/reports/{report.id}/metrics/{METRIC_KEY}/evidence",
        headers=headers,
        params={"limit": 500},
    )
    assert too_large.status_code == 422

    bad_page = client.get(
        f"/reports/{report.id}/metrics/{METRIC_KEY}/evidence",
        headers=headers,
        params={"page": 0},
    )
    assert bad_page.status_code == 422
