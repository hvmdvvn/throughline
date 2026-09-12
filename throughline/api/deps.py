"""FastAPI dependencies (DB session, etc.)."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from throughline.db.session import get_session_factory


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy session."""
    session_factory = get_session_factory()
    with session_factory() as session:
        yield session
