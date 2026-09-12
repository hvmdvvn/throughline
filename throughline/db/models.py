"""ORM models.

Identity / tenancy (issue #6): ``Org``, ``User``, ``Membership``.
``DevPgvectorProof`` is a disposable foundation table used only to prove
pgvector round-trips (issue #3). It is not a product embeddings table.
"""

from __future__ import annotations

import enum
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from throughline.db.base import Base, SoftDeleteMixin, TenantScopedMixin, TimestampMixin

# Small fixed dimension for the throwaway proof; real embeddings come later.
DEV_PGVECTOR_PROOF_DIM = 3


class MembershipRole(enum.StrEnum):
    """Roles allowed on an org membership (plan §3 / issue #6)."""

    ADMIN = "admin"
    PM = "pm"
    VIEWER = "viewer"


class Org(TimestampMixin, SoftDeleteMixin, Base):
    """Organization (tenant root). No ``org_id`` on this table (plan §3)."""

    __tablename__ = "orgs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="org",
        cascade="all, delete-orphan",
    )


class User(TimestampMixin, SoftDeleteMixin, Base):
    """In-app person identity.

    ``auth_subject`` is the hosted-auth provider subject (Clerk ``sub``, issue #8).
    Users are not tenant-scoped rows; org linkage is via ``Membership``.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    auth_subject: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )
    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Membership(TenantScopedMixin, Base):
    """Links a ``User`` to an ``Org`` with a constrained role."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_memberships_org_user"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[MembershipRole] = mapped_column(
        Enum(
            MembershipRole,
            name="membership_role",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=True,
        ),
        nullable=False,
    )

    org: Mapped[Org] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class DevPgvectorProof(Base):
    """Throwaway row used to verify insert/select of a pgvector column."""

    __tablename__ = "dev_pgvector_proof"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(DEV_PGVECTOR_PROOF_DIM), nullable=False)
