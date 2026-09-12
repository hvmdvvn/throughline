"""SQLAlchemy declarative base for Throughline models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared metadata base for Alembic and ORM models."""
