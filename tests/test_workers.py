"""arq worker jobs, retry/backoff config, and admin job status (issue #9)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from arq import Retry
from arq.connections import RedisSettings
from arq.jobs import JobStatus
from arq.worker import Worker, func
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from sqlalchemy.exc import SQLAlchemyError

from throughline.api.app import app
from throughline.api.auth import clear_jwks_cache
from throughline.config import settings
from throughline.db.models import Membership, Org, User
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url
from throughline.workers.jobs import (
    EXAMPLE_JOB_BACKOFF_BASE_SECONDS,
    EXAMPLE_JOB_KEEP_RESULT_SECONDS,
    EXAMPLE_JOB_MAX_TRIES,
    example_sleep_log,
    exponential_backoff_seconds,
)
from throughline.workers.pool import arq_redis_pool, redis_settings_from_config
from throughline.workers.settings import WorkerSettings, example_sleep_log_job, ping

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_ISSUER = "https://throughline-test.clerk.accounts.dev"
TEST_KID = "test-key-workers"


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


def mint_token(private_key, *, sub: str) -> str:
    now = int(time.time())
    payload = {
        "sub": sub,
        "iss": TEST_ISSUER,
        "iat": now - 10,
        "exp": now + 3600,
        "email": f"{sub}@example.com",
        "name": "Worker Test",
    }
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": TEST_KID})


def _redis_reachable() -> bool:
    try:
        from throughline.connectivity import check_redis

        check_redis()
        return True
    except OSError:
        return False


def _postgres_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except (OSError, SQLAlchemyError):
        return False


def _alembic_config() -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_database_url())
    return cfg


@pytest.fixture(scope="module")
def migrated_db() -> None:
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose / CI)")
    command.upgrade(_alembic_config(), "head")


@pytest.fixture
def clean_identity(migrated_db: None):
    Session = get_session_factory()
    with Session() as session:
        session.execute(delete(Membership))
        session.execute(delete(User))
        session.execute(delete(Org))
        session.commit()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_worker_registers_example_job() -> None:
    assert ping in WorkerSettings.functions
    assert example_sleep_log_job in WorkerSettings.functions
    assert isinstance(WorkerSettings.redis_settings, RedisSettings)


def test_no_celery_task_queue() -> None:
    import importlib.util

    assert importlib.util.find_spec("celery") is None
    assert importlib.util.find_spec("django_rq") is None


def test_retry_backoff_configuration_loaded() -> None:
    """Documented check: keep_result / max_tries / exponential backoff present."""
    assert WorkerSettings.max_tries == EXAMPLE_JOB_MAX_TRIES
    assert WorkerSettings.keep_result == EXAMPLE_JOB_KEEP_RESULT_SECONDS
    assert WorkerSettings.retry_jobs is True
    assert example_sleep_log_job.max_tries == EXAMPLE_JOB_MAX_TRIES
    assert example_sleep_log_job.keep_result_s == EXAMPLE_JOB_KEEP_RESULT_SECONDS

    assert exponential_backoff_seconds(1) == EXAMPLE_JOB_BACKOFF_BASE_SECONDS
    assert exponential_backoff_seconds(2) == EXAMPLE_JOB_BACKOFF_BASE_SECONDS * 2
    assert exponential_backoff_seconds(3) == EXAMPLE_JOB_BACKOFF_BASE_SECONDS * 4
    retry = Retry(defer=exponential_backoff_seconds(4))
    assert retry.defer_score == int(EXAMPLE_JOB_BACKOFF_BASE_SECONDS * 8 * 1000)


def test_redis_settings_match_project_config() -> None:
    rs = redis_settings_from_config()
    assert isinstance(rs, RedisSettings)
    assert WorkerSettings.redis_settings.host == rs.host
    assert WorkerSettings.redis_settings.port == rs.port


def test_example_job_enqueues_and_completes() -> None:
    if not _redis_reachable():
        pytest.skip("Redis not reachable (run via docker compose / CI)")

    async def _run() -> None:
        async with arq_redis_pool() as redis:
            job = await redis.enqueue_job("example_sleep_log")
            assert job is not None
            worker = Worker(
                functions=WorkerSettings.functions,
                redis_pool=redis,
                burst=True,
                poll_delay=0.05,
                max_tries=WorkerSettings.max_tries,
                keep_result=WorkerSettings.keep_result,
                retry_jobs=WorkerSettings.retry_jobs,
            )
            await worker.async_run()
            assert await job.status() is JobStatus.complete
            assert await job.result(timeout=5) == {"ok": True, "job_try": 1}

    asyncio.run(_run())


def test_admin_job_status_requires_auth(client: TestClient, clean_identity) -> None:
    assert client.get("/admin/jobs/does-not-exist").status_code == 401
    assert (
        client.get(
            "/admin/jobs/does-not-exist",
            headers={"Authorization": "Bearer not-a-jwt"},
        ).status_code
        == 401
    )


def test_admin_job_status_returns_terminal_status(
    client: TestClient, clean_identity, rsa_keypair
) -> None:
    if not _redis_reachable():
        pytest.skip("Redis not reachable (run via docker compose / CI)")
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose / CI)")

    private_key, _ = rsa_keypair
    token = mint_token(private_key, sub=f"clerk_job_status_{uuid.uuid4().hex[:8]}")
    headers = {"Authorization": f"Bearer {token}"}

    missing = client.get("/admin/jobs/missing-job-id-xyz", headers=headers)
    assert missing.status_code == 200
    body = missing.json()
    assert body["job_id"] == "missing-job-id-xyz"
    assert body["status"] == JobStatus.not_found.value

    async def _enqueue_and_run() -> str:
        async with arq_redis_pool() as redis:
            job = await redis.enqueue_job("example_sleep_log")
            assert job is not None
            worker = Worker(
                functions=[func(example_sleep_log, name="example_sleep_log")],
                redis_pool=redis,
                burst=True,
                poll_delay=0.05,
                keep_result=EXAMPLE_JOB_KEEP_RESULT_SECONDS,
            )
            await worker.async_run()
            assert await job.status() is JobStatus.complete
            return job.job_id

    job_id = asyncio.run(_enqueue_and_run())
    response = client.get(f"/admin/jobs/{job_id}", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"] == job_id
    assert payload["status"] == JobStatus.complete.value
    assert payload["success"] is True
    assert payload["result"] == {"ok": True, "job_try": 1}
    assert payload["function"] == "example_sleep_log"
