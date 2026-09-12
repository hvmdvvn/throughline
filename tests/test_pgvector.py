"""pgvector round-trip proof against Compose Postgres (issue #3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, select, text

from throughline.db.models import DEV_PGVECTOR_PROOF_DIM, DevPgvectorProof
from throughline.db.session import get_engine, get_session_factory, sqlalchemy_database_url

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
    except Exception:
        return False


@pytest.fixture(scope="module")
def migrated_db() -> None:
    """Apply Alembic migrations when Postgres is available; otherwise skip."""
    if not _postgres_reachable():
        pytest.skip("Postgres not reachable (run via docker compose)")
    command.upgrade(_alembic_config(), "head")


def test_pgvector_extension_available(migrated_db: None) -> None:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        ).one_or_none()
    assert row is not None


def test_pgvector_round_trip(migrated_db: None) -> None:
    """Insert a vector and read it back with element-wise equality."""
    assert DEV_PGVECTOR_PROOF_DIM == 3
    vector = [0.1, -0.2, 0.3]
    Session = get_session_factory()

    with Session() as session:
        session.execute(delete(DevPgvectorProof))
        session.commit()

        row = DevPgvectorProof(embedding=vector)
        session.add(row)
        session.commit()
        row_id = row.id

    with Session() as session:
        loaded = session.scalar(
            select(DevPgvectorProof).where(DevPgvectorProof.id == row_id)
        )
        assert loaded is not None
        # pgvector may return list or numpy-like; compare as floats.
        retrieved = [float(x) for x in loaded.embedding]
        assert retrieved == pytest.approx(vector)
