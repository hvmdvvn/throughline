"""ORM models.

Identity / tenancy (issue #6): ``Org``, ``User``, ``Membership``.
Jira OAuth connection (issue #10): ``JiraConnection`` — encrypted per-org tokens.
Jira discovery (issue #12): ``Project``, issue types / statuses / fields, field mappings.
History import (issue #13): ``JiraIssue``, ``SyncState``; import progress on ``JiraConnection``.
Changelog import (issue #14): ``JiraStatusTransition``; sibling sync_state + connection progress.
Canonical normalization (issue #15): ``Issue``, ``IssueTransition`` — no Jira field ids.
Cycle time outcomes (issue #17): ``Outcome`` — stored time-in-status / cycle time.
Reopen metrics (issue #18): ``ReopenEvent``, ``ReopenAggregate`` — done→active
detections with evidence refs; frequency aggregates by epic/project/month.
``DevPgvectorProof`` is a disposable foundation table used only to prove
pgvector round-trips (issue #3). It is not a product embeddings table.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    # Issue history import progress (issue #13) — mirrored from sync_state for ops.
    import_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    import_imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    import_total_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    import_cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    import_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Changelog status-transition pass (issue #14) — sibling cursor mirrored for ops.
    changelog_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    changelog_imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    changelog_total_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    changelog_cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    changelog_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


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


class SyncRunStatus(enum.StrEnum):
    """Lifecycle for a connector sync_state row (plan §3 / issue #13)."""

    IDLE = "idle"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SyncState(TenantScopedMixin, Base):
    """Per-connector cursors, last-seen, and backoff state (plan §3).

    Issue history import uses ``connector=jira`` / ``sync_key=issue_history``.
    Cursor is the next ``startAt`` offset (stringified int) after the last
    successfully committed page so an interrupted worker resumes mid-run.

    Changelog import uses ``sync_key=changelog``; cursor is the last successfully
    processed ``issue_key`` (ordered ascending) so a mid-run failure resumes
    without re-fetching already completed issues.
    """

    __tablename__ = "sync_states"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "connector",
            "sync_key",
            name="uq_sync_states_org_connector_key",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    connector: Mapped[str] = mapped_column(String(64), nullable=False)
    sync_key: Mapped[str] = mapped_column(String(128), nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[SyncRunStatus] = mapped_column(
        Enum(
            SyncRunStatus,
            name="sync_run_status",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
            native_enum=True,
        ),
        nullable=False,
        default=SyncRunStatus.IDLE,
    )
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    backoff_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class JiraIssue(TenantScopedMixin, Base):
    """Imported Jira issue snapshot for Phase 0 history (plan §3 / issue #13).

    Upserted on ``(org_id, issue_key)``. ``node_id`` links to requirement nodes
    later. Soft-delete only. ``changelog_imported_at`` marks a finished
    changelog pass for the issue (issue #14).
    """

    __tablename__ = "jira_issues"
    __table_args__ = (
        UniqueConstraint("org_id", "issue_key", name="uq_jira_issues_org_issue_key"),
        UniqueConstraint("org_id", "external_id", name="uq_jira_issues_org_external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issue_type_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_type_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Full search payload for canonical normalization (#15).
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    jira_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    jira_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Set when changelog status transitions for this issue are fully stored (#14).
    changelog_imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Optional link to a requirement node (Phase 1); unused in Phase 0 import.
    node_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class JiraStatusTransition(TenantScopedMixin, Base):
    """Append-oriented status transition from a Jira changelog history (issue #14).

    Deduped on ``(org_id, issue_key, history_id, item_index)`` so re-runs and
    mid-issue retries do not duplicate rows used later for cycle time / reopen.
    """

    __tablename__ = "jira_status_transitions"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "issue_key",
            "history_id",
            "item_index",
            name="uq_jira_status_transitions_org_issue_history_item",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False)
    history_id: Mapped[str] = mapped_column(String(64), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    from_status_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    from_status_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_status_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_status_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Issue(TenantScopedMixin, Base):
    """Canonical issue row for analytics — independent of Jira field ids (issue #15).

    Populated by the ingest mapping layer from connector-stored payloads using
    per-org field mappings. Soft-delete only.
    """

    __tablename__ = "issues"
    __table_args__ = (
        UniqueConstraint("org_id", "external_key", name="uq_issues_org_external_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # Connector-facing issue identifier (e.g. Jira issue key) — not a custom field id.
    external_key: Mapped[str] = mapped_column(String(64), nullable=False)
    project_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Parent epic external key when known (Phase 0 reopen aggregates; optional).
    epic_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    acceptance_criteria: Mapped[str | None] = mapped_column(Text, nullable=True)
    story_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class IssueTransition(TenantScopedMixin, Base):
    """Canonical status transition for analytics (issue #15).

    ``external_event_id`` / ``event_index`` are opaque connector event coordinates
    for idempotent upserts — not Jira custom-field names.
    """

    __tablename__ = "issue_transitions"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "external_key",
            "external_event_id",
            "event_index",
            name="uq_issue_transitions_org_key_event_item",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_key: Mapped[str] = mapped_column(String(64), nullable=False)
    transitioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)


class Outcome(TenantScopedMixin, Base):
    """Stored per-issue cycle time and time-in-status (issue #17).

    Computed from canonical ``issue_transitions`` and persisted so Phase 0
    analytics are not recalculated only at read time. Soft-delete only.
    ``transitions_fingerprint`` versions the source transition set for
    idempotent re-runs.
    """

    __tablename__ = "outcomes"
    __table_args__ = (
        UniqueConstraint("org_id", "external_key", name="uq_outcomes_org_external_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Status name → total seconds spent in that status (closed intervals only).
    time_in_status_seconds: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # First completed in-progress→done pass duration; null if never completed.
    cycle_time_seconds: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    pass_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # List of {started_at, done_at, duration_seconds} for each completed pass.
    cycle_passes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    first_in_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    first_done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_done_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    transitions_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class ReopenEvent(TenantScopedMixin, Base):
    """One done→active status transition detection (issue #18).

    Evidence coordinates (``evidence_external_event_id`` / ``evidence_event_index``)
    match canonical ``issue_transitions`` so UI drill-down stays stable.
    Soft-delete only. No cause or blame fields.
    """

    __tablename__ = "reopen_events"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "external_key",
            "evidence_external_event_id",
            "evidence_event_index",
            name="uq_reopen_events_org_key_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Empty string when unknown — keeps unique aggregates simple.
    project_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    epic_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    transitioned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evidence_external_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Stable drill-down token: issue:{key}/transition:{event_id}:{index}
    evidence_ref: Mapped[str] = mapped_column(String(255), nullable=False)


class ReopenAggregate(TenantScopedMixin, Base):
    """Reopen frequency rollup by epic or project and calendar month (issue #18).

    Counts only — no causal labels. Soft-delete only.
    """

    __tablename__ = "reopen_aggregates"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "period_start",
            "period_grain",
            "dimension",
            "dimension_key",
            name="uq_reopen_aggregates_org_period_dim",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_grain: Mapped[str] = mapped_column(String(16), nullable=False, default="month")
    # ``epic`` or ``project``.
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    dimension_key: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reopen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DevPgvectorProof(Base):
    """Throwaway row used to verify insert/select of a pgvector column."""

    __tablename__ = "dev_pgvector_proof"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(DEV_PGVECTOR_PROOF_DIM), nullable=False)
