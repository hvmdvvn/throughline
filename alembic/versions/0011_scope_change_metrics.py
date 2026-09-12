"""Scope change: late children + post-start description/AC edits (issue #19).

Revision ID: 0011_scope_change_metrics
Revises: 0010_reopen_metrics
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_scope_change_metrics"
down_revision: str | None = "0010_reopen_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issue_field_changes",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_key", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("external_event_id", sa.String(length=64), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "external_key",
            "external_event_id",
            "event_index",
            name="uq_issue_field_changes_org_key_event_item",
        ),
    )
    op.create_index(
        "ix_issue_field_changes_org_id",
        "issue_field_changes",
        ["org_id"],
        unique=False,
    )

    op.create_table(
        "late_child_events",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("epic_key", sa.String(length=64), nullable=False),
        sa.Column("external_key", sa.String(length=64), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("child_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("epic_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delay_seconds", sa.BigInteger(), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "epic_key",
            "external_key",
            name="uq_late_child_events_org_epic_child",
        ),
    )
    op.create_index("ix_late_child_events_org_id", "late_child_events", ["org_id"], unique=False)

    op.create_table(
        "spec_change_events",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_key", sa.String(length=64), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("epic_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("work_began_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_external_event_id", sa.String(length=64), nullable=False),
        sa.Column("evidence_event_index", sa.Integer(), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "external_key",
            "evidence_external_event_id",
            "evidence_event_index",
            name="uq_spec_change_events_org_key_evidence",
        ),
    )
    op.create_index(
        "ix_spec_change_events_org_id",
        "spec_change_events",
        ["org_id"],
        unique=False,
    )

    op.create_table(
        "scope_change_aggregates",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_grain", sa.String(length=16), nullable=False, server_default="month"),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("dimension_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("late_child_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("spec_change_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "period_start",
            "period_grain",
            "dimension",
            "dimension_key",
            name="uq_scope_change_aggregates_org_period_dim",
        ),
    )
    op.create_index(
        "ix_scope_change_aggregates_org_id",
        "scope_change_aggregates",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_scope_change_aggregates_org_id", table_name="scope_change_aggregates")
    op.drop_table("scope_change_aggregates")
    op.drop_index("ix_spec_change_events_org_id", table_name="spec_change_events")
    op.drop_table("spec_change_events")
    op.drop_index("ix_late_child_events_org_id", table_name="late_child_events")
    op.drop_table("late_child_events")
    op.drop_index("ix_issue_field_changes_org_id", table_name="issue_field_changes")
    op.drop_table("issue_field_changes")
