"""SQLAlchemy declarative base and shared mixins for Throughline models."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared metadata base for Alembic and ORM models."""


class TimestampMixin:
    """created_at / updated_at columns for all durable rows."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Soft-delete flag: NULL means active; set to hide from default queries."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
    )


class TenantScopedMixin(TimestampMixin, SoftDeleteMixin):
    """Mixin for every tenant table except ``orgs`` (plan §3).

    Establishes ``org_id`` + timestamps + soft-delete for Membership and all
    future org-scoped tables. Cross-tenant query enforcement is issue #7.
    """

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
