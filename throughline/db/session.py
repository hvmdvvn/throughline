"""Engine and session helpers for Postgres (psycopg3)."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from throughline.config import settings


def sqlalchemy_database_url(url: str | None = None) -> str:
    """Normalize DATABASE_URL for SQLAlchemy + psycopg3.

    Compose and settings use ``postgresql://...``; SQLAlchemy needs the
    ``postgresql+psycopg://`` dialect when using the psycopg3 driver.
    """
    raw = url if url is not None else settings.database_url
    if raw.startswith("postgresql+psycopg://"):
        return raw
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw.removeprefix("postgres://")
    return raw


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return a process-wide sync engine (Alembic + tests)."""
    return create_engine(sqlalchemy_database_url(), pool_pre_ping=True)


def get_session_factory() -> sessionmaker[Session]:
    """Return a session factory bound to the shared engine."""
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)
