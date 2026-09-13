"""Diagnostic onboarding flow: OAuth → import → changelog → report → email (#27).

Happy-path and failure transitions use fixtures/mocks — no live Jira or SMTP.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import delete, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.app import app
from throughline.api.auth import clear_jwks_cache
from throughline.config import settings
from throughline.connectors.jira import oauth as jira_oauth_http
from throughline.connectors.jira.crypto import encrypt_secret
from throughline.connectors.jira.import_changelog import ChangelogImportResult
from throughline.connectors.jira.import_history import ImportResult
from throughline.connectors.jira.service import create_oauth_state
from throughline.db.models import (
    DiagnosticOnboarding,
    DiagnosticOnboardingStage,
    DiagnosticReport,
    DiagnosticReportStatus,
    JiraConnection,
    JiraConnectionStatus,
    Membership,
    MembershipRole,
    Org,
    User,
)
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.email.smtp import EmailDeliveryResult, send_email
from throughline.onboarding.diagnostic import (
    advance_after_oauth,
    reset_for_retry,
    run_pipeline,
    start_or_resume,
)
from throughline.tenancy import use_org
from throughline.workers.jobs import run_diagnostic_onboarding
from throughline.workers.settings import WorkerSettings

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-onboarding"
FERNET_KEY = "dGhyb3VnaGxpbmUtZGV2LWZlcm5ldC1rZXktMzJiISE="
RANGE_START = date(2024, 6, 1)
RANGE_END = date(2024, 6, 30)


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
    monkeypatch.setattr(settings, "atlassian_client_id", "test-client")
    monkeypatch.setattr(settings, "atlassian_client_secret", "test-secret")
    monkeypatch.setattr(
        settings,
        "atlassian_redirect_uri",
        "http://localhost:8000/connectors/jira/oauth/callback",
    )
    monkeypatch.setattr(settings, "web_app_url", "")
    monkeypatch.setattr(settings, "email_enabled", False)
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
        "name": "Onboarding User",
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
def client():
    return TestClient(app)


def _wipe(db_session) -> None:
    db_session.execute(delete(DiagnosticOnboarding))
    db_session.execute(delete(DiagnosticReport))
    db_session.execute(delete(JiraConnection))
    db_session.execute(delete(Membership))
    db_session.execute(delete(User))
    db_session.execute(delete(Org))
    db_session.commit()


@pytest.fixture
def org_ready(db_session):
    _wipe(db_session)
    org = Org(name="Onboarding Org")
    user = User(auth_subject="onboard-admin-sub", email="admin@example.com")
    db_session.add_all([org, user])
    db_session.flush()
    db_session.add(Membership(org_id=org.id, user_id=user.id, role=MembershipRole.ADMIN))
    db_session.commit()
    return org, user


@pytest.fixture
def auth_header(org_ready, rsa_keypair):
    org, user = org_ready
    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub=user.auth_subject or "onboard-admin-sub")
    return org, {"Authorization": f"Bearer {token}", "X-Org-Id": str(org.id)}


def _connect_jira(db_session, org) -> JiraConnection:
    connection = JiraConnection(
        org_id=org.id,
        cloud_id="cloud-onboard",
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


def _persist_report(db, org_id: uuid.UUID) -> DiagnosticReport:
    report = DiagnosticReport(
        org_id=org_id,
        range_start=RANGE_START,
        range_end=RANGE_END,
        version=1,
        status=DiagnosticReportStatus.SUCCESS.value,
        metrics={"reopen.event_count": {"value": 1, "evidence_refs": ["ISSUE-1"]}},
        generation_detail={"reopen": {"ok": True}},
        generated_at=datetime.now(UTC),
    )
    db.add(report)
    db.flush()
    return report


class _FakeJob:
    def __init__(self, job_id: str = "job-onboarding-1"):
        self.job_id = job_id


class _FakeRedis:
    def __init__(self, job: _FakeJob | None = None):
        self.job = job or _FakeJob()
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, name: str, *args):
        self.enqueued.append((name, args))
        return self.job


def _patch_arq(fake: _FakeRedis):
    @asynccontextmanager
    async def _pool():
        yield fake

    return patch("throughline.api.routes.onboarding.arq_redis_pool", _pool)


def test_diagnostic_onboardings_table_migrated(migrated_db) -> None:
    inspector = inspect(get_engine())
    assert "diagnostic_onboardings" in inspector.get_table_names()
    cols = {c["name"] for c in inspector.get_columns("diagnostic_onboardings")}
    assert {
        "stage",
        "notify_email",
        "progress_status",
        "orchestrator_job_id",
        "report_id",
        "email_status",
        "failed_stage",
        "error_message",
    }.issubset(cols)


def test_worker_registers_onboarding_job() -> None:
    names = {
        getattr(f, "name", None) or getattr(f, "__name__", str(f))
        for f in WorkerSettings.functions
    }
    assert "run_diagnostic_onboarding" in names


def test_start_awaits_oauth_when_disconnected(db_session, org_ready):
    org, _ = org_ready
    with use_org(org.id):
        session, needs_job = start_or_resume(
            db_session,
            org.id,
            notify_email="pm@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
    assert needs_job is False
    assert session.stage == DiagnosticOnboardingStage.AWAITING_OAUTH.value


def test_happy_path_pipeline_state_transitions(db_session, org_ready, monkeypatch):
    org, _ = org_ready
    _connect_jira(db_session, org)
    emails: list[dict] = []

    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_issue_history_import_for_org",
        lambda *a, **k: ImportResult(10, 10, "10", True, 1),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_changelog_import_for_org",
        lambda *a, **k: ChangelogImportResult(10, 10, "Z", True, 10, 5),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.generate_diagnostic_report",
        lambda db, org_id, *_a, **_k: _persist_report(db, org_id),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.send_email",
        lambda **kw: emails.append(kw) or EmailDeliveryResult(status="sent"),
    )

    with use_org(org.id):
        session, needs_job = start_or_resume(
            db_session,
            org.id,
            notify_email="ready@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
        assert needs_job is True
        result = run_pipeline(db_session, session.id)
        db_session.refresh(session)

    assert result["ok"] is True
    assert result["completed"] is True
    assert len(emails) == 1
    assert emails[0]["to"] == "ready@example.com"
    assert session.stage == DiagnosticOnboardingStage.COMPLETED.value
    assert session.report_id is not None
    assert session.email_status == "sent"
    assert session.error_message is None


def test_slow_import_needs_continue_then_completes(db_session, org_ready, monkeypatch):
    org, _ = org_ready
    _connect_jira(db_session, org)
    issue_calls = {"n": 0}

    def fake_issues(*_a, **_k):
        issue_calls["n"] += 1
        if issue_calls["n"] == 1:
            return ImportResult(2, 10, "2", False, 1)
        return ImportResult(10, 10, "10", True, 1)

    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_issue_history_import_for_org",
        fake_issues,
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_changelog_import_for_org",
        lambda *a, **k: ChangelogImportResult(10, 10, None, True, 10, 5),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.generate_diagnostic_report",
        lambda db, org_id, *_a, **_k: _persist_report(db, org_id),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.send_email",
        lambda **_k: EmailDeliveryResult(status="skipped", detail="disabled"),
    )

    with use_org(org.id):
        session, _ = start_or_resume(
            db_session,
            org.id,
            notify_email="slow@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
        first = run_pipeline(db_session, session.id)
        assert first.get("needs_continue") is True
        db_session.refresh(session)
        assert session.stage == DiagnosticOnboardingStage.IMPORTING_ISSUES.value
        assert session.progress_imported_count == 2

        second = run_pipeline(db_session, session.id)
        db_session.refresh(session)

    assert second["completed"] is True
    assert session.stage == DiagnosticOnboardingStage.COMPLETED.value
    assert issue_calls["n"] == 2


def test_failure_sets_recoverable_failed_stage(db_session, org_ready, monkeypatch):
    org, _ = org_ready
    _connect_jira(db_session, org)
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_issue_history_import_for_org",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Jira import exploded")),
    )

    with use_org(org.id):
        session, _ = start_or_resume(
            db_session,
            org.id,
            notify_email="fail@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
        result = run_pipeline(db_session, session.id)
        db_session.refresh(session)
        assert result["ok"] is False
        assert session.stage == DiagnosticOnboardingStage.FAILED.value
        assert session.failed_stage == DiagnosticOnboardingStage.IMPORTING_ISSUES.value

        session, needs = reset_for_retry(db_session)
        db_session.commit()
        assert needs is True
        assert session.stage == DiagnosticOnboardingStage.IMPORTING_ISSUES.value
        assert session.error_message is None


def test_arq_job_requeues_on_needs_continue(db_session, org_ready, monkeypatch):
    org, _ = org_ready
    _connect_jira(db_session, org)

    with use_org(org.id):
        session, _ = start_or_resume(
            db_session,
            org.id,
            notify_email="requeue@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
        session_id = str(session.id)

    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_issue_history_import_for_org",
        lambda *a, **k: ImportResult(1, 5, "1", False, 1),
    )

    redis = _FakeRedis(_FakeJob("requeue-2"))

    @asynccontextmanager
    async def _pool():
        yield redis

    monkeypatch.setattr("throughline.workers.jobs.arq_redis_pool", _pool)

    result = asyncio.run(run_diagnostic_onboarding({}, session_id))
    assert result.get("needs_continue") is True
    assert result.get("requeued") is True
    assert redis.enqueued[0][0] == "run_diagnostic_onboarding"


def test_api_start_continue_poll_retry(client, db_session, org_ready, auth_header):
    org, headers = auth_header
    fake_redis = _FakeRedis()

    with _patch_arq(fake_redis):
        start = client.post(
            "/onboarding/diagnostic",
            headers=headers,
            json={
                "notify_email": "guide@example.com",
                "range_start": RANGE_START.isoformat(),
                "range_end": RANGE_END.isoformat(),
            },
        )
    assert start.status_code == 200
    body = start.json()
    assert body["stage"] == DiagnosticOnboardingStage.AWAITING_OAUTH.value
    assert body["authorize_url"]
    assert fake_redis.enqueued == []

    poll = client.get("/onboarding/diagnostic", headers=headers)
    assert poll.status_code == 200
    assert poll.json()["stage"] == DiagnosticOnboardingStage.AWAITING_OAUTH.value

    _connect_jira(db_session, org)

    with _patch_arq(fake_redis):
        cont = client.post("/onboarding/diagnostic/continue", headers=headers)
    assert cont.status_code == 200
    assert cont.json()["stage"] == DiagnosticOnboardingStage.IMPORTING_ISSUES.value
    assert fake_redis.enqueued[-1][0] == "run_diagnostic_onboarding"
    assert cont.json()["orchestrator_job_id"] == "job-onboarding-1"

    with use_org(org.id):
        session = db_session.get(DiagnosticOnboarding, uuid.UUID(cont.json()["id"]))
        assert session is not None
        session.stage = DiagnosticOnboardingStage.FAILED.value
        session.failed_stage = DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value
        session.error_message = "simulated"
        session.progress_status = "failed"
        db_session.commit()

    assert client.get("/onboarding/diagnostic", headers=headers).json()["recoverable"] is True

    with _patch_arq(fake_redis):
        retry = client.post("/onboarding/diagnostic/retry", headers=headers)
    assert retry.status_code == 200
    assert retry.json()["stage"] == DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value
    assert retry.json()["error_message"] is None


def test_leave_and_return_does_not_reset_running(db_session, org_ready):
    org, _ = org_ready
    _connect_jira(db_session, org)
    with use_org(org.id):
        session, _ = start_or_resume(
            db_session,
            org.id,
            notify_email="leave@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        session.stage = DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value
        session.progress_detail = "mid-run"
        db_session.commit()

        again, needs = start_or_resume(
            db_session,
            org.id,
            notify_email="other@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
    assert needs is False
    assert again.stage == DiagnosticOnboardingStage.IMPORTING_CHANGELOG.value
    assert again.notify_email == "leave@example.com"


def test_advance_after_oauth_requires_connection(db_session, org_ready):
    org, _ = org_ready
    with use_org(org.id):
        start_or_resume(
            db_session,
            org.id,
            notify_email="oauth@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
        session, needs = advance_after_oauth(db_session, org.id)
    assert needs is False
    assert session.stage == DiagnosticOnboardingStage.AWAITING_OAUTH.value


def test_email_skipped_still_completes(db_session, org_ready, monkeypatch):
    org, _ = org_ready
    _connect_jira(db_session, org)
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_issue_history_import_for_org",
        lambda *a, **k: ImportResult(10, 10, "10", True, 1),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.run_changelog_import_for_org",
        lambda *a, **k: ChangelogImportResult(10, 10, None, True, 10, 1),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.generate_diagnostic_report",
        lambda db, org_id, *_a, **_k: _persist_report(db, org_id),
    )
    monkeypatch.setattr(
        "throughline.onboarding.diagnostic.send_email",
        lambda **_k: EmailDeliveryResult(
            status="skipped",
            detail="EMAIL_ENABLED is false; outbound email disabled",
        ),
    )
    with use_org(org.id):
        session, _ = start_or_resume(
            db_session,
            org.id,
            notify_email="skip@example.com",
            range_start=RANGE_START,
            range_end=RANGE_END,
        )
        db_session.commit()
        result = run_pipeline(db_session, session.id)
        db_session.refresh(session)
    assert result["ok"] is True
    assert session.stage == DiagnosticOnboardingStage.COMPLETED.value
    assert session.email_status == "skipped"


def test_send_email_skipped_when_disabled():
    result = send_email(to="a@b.com", subject="x", body_text="y")
    assert result.status == "skipped"


def test_oauth_callback_redirects_to_web_when_configured(
    client, db_session, org_ready, monkeypatch
):
    org, _ = org_ready
    monkeypatch.setattr(settings, "web_app_url", "http://localhost:3000")
    state = create_oauth_state(org.id)

    class _FakeHTTPResponse:
        def __init__(self, payload: object):
            self._body = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

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
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "http://localhost:3000/onboarding?jira=connected"
    )


def test_onboarding_requires_auth(client):
    assert client.get("/onboarding/diagnostic").status_code == 401
