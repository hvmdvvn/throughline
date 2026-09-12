"""ORM models.

Identity / tenancy (issue #6): ``Org``, ``User``, ``Membership``.
Jira OAuth connection (issue #10): ``JiraConnection`` — encrypted per-org tokens.
Jira discovery (issue #12): ``Project``, issue types / statuses / fields, field mappings.
``DevPgvectorProof`` is a disposable foundation table used only to prove
pgvector round-trips (issue #3). It is not a product embeddings table.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
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


class JiraConnectionStatus(enum.StrEnum):
    """Lifecycle state for an org's Jira Cloud OAuth connection."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class JiraConnection(TenantScopedMixin, Base):
    """Per-org Atlassian OAuth credentials (encrypted) for Jira Cloud.

    One row per org (unique ``org_id``). Soft-delete + ``status`` cover disconnect;
    failed refresh sets ``status=error`` with an ops-safe ``status_detail``.
    """

    __tablename__ = "jira_connections"
    __table_args__ = (UniqueConstraint("org_id", name="uq_jira_connections_org"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    cloud_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    site_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    site_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Fernet ciphertext — never store or log plaintext tokens.
    encrypted_access_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    encrypted_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[JiraConnectionStatus] = mapped_column(
        Enum(
            JiraConnectionStatus,
            name="jira_connection_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=True,
        ),
        nullable=False,
        default=JiraConnectionStatus.DISCONNECTED,
    )
    status_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)


class JiraFieldConcept(enum.StrEnum):
    """Canonical concepts Phase 0 analytics resolve via per-org field mappings.

    Application code looks up fields by these concepts — never by Jira field
    ids or display names hardcoded in business logic (plan §5 / issue #12).
    """

    ACCEPTANCE_CRITERIA = "acceptance_criteria"
    STORY_POINTS = "story_points"


class Project(TenantScopedMixin, Base):
    """Org project mirroring a connected Jira project (plan §3 / issue #12).

    ``external_id`` is the connector's project id (Jira numeric id as string).
    Re-discovery upserts on ``(org_id, external_id)`` so projects are not
    duplicated as separate tenants.
    """

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("org_id", "external_id", name="uq_projects_org_external_id"),
        UniqueConstraint("org_id", "key", name="uq_projects_org_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class JiraIssueType(TenantScopedMixin, Base):
    """Discovered Jira issue type definition for an org (issue #12)."""

    __tablename__ = "jira_issue_types"
    __table_args__ = (
        UniqueConstraint("org_id", "external_id", name="uq_jira_issue_types_org_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    hierarchy_level: Mapped[int | None] = mapped_column(Integer, nullable=True)


class JiraStatus(TenantScopedMixin, Base):
    """Discovered Jira status definition for an org (issue #12)."""

    __tablename__ = "jira_statuses"
    __table_args__ = (
        UniqueConstraint("org_id", "external_id", name="uq_jira_statuses_org_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status_category_key: Mapped[str | None] = mapped_column(String(64), nullable=True)


class JiraFieldDefinition(TenantScopedMixin, Base):
    """Discovered Jira field (system or custom) for an org (issue #12)."""

    __tablename__ = "jira_field_definitions"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "field_id",
            name="uq_jira_field_definitions_org_field_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Jira field key, e.g. customfield_10016 — stored, never hardcoded in app logic.
    field_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    schema_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    schema_custom: Mapped[str | None] = mapped_column(String(255), nullable=True)


class JiraFieldMapping(TenantScopedMixin, Base):
    """Per-org mapping from a canonical concept to a discovered Jira field id."""

    __tablename__ = "jira_field_mappings"
    __table_args__ = (
        UniqueConstraint("org_id", "concept", name="uq_jira_field_mappings_org_concept"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    concept: Mapped[JiraFieldConcept] = mapped_column(
        Enum(
            JiraFieldConcept,
            name="jira_field_concept",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=True,
        ),
        nullable=False,
    )
    jira_field_id: Mapped[str] = mapped_column(String(128), nullable=False)


class DevPgvectorProof(Base):
    """Throwaway row used to verify insert/select of a pgvector column."""

    __tablename__ = "dev_pgvector_proof"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(DEV_PGVECTOR_PROOF_DIM), nullable=False)
