"""Reopen event evidence + epic/project/month aggregates (issue #18).

Revision ID: 0010_reopen_metrics
Revises: 0009_outcomes_cycle_time
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_reopen_metrics"
down_revision: str | None = "0009_outcomes_cycle_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("issues", sa.Column("epic_key", sa.String(length=64), nullable=True))

    op.create_table(
        "reopen_events",
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
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_status", sa.String(length=255), nullable=True),
        sa.Column("to_status", sa.String(length=255), nullable=True),
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
            name="uq_reopen_events_org_key_evidence",
        ),
    )
    op.create_index("ix_reopen_events_org_id", "reopen_events", ["org_id"], unique=False)

    op.create_table(
        "reopen_aggregates",
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
        sa.Column("reopen_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "period_start",
            "period_grain",
            "dimension",
            "dimension_key",
            name="uq_reopen_aggregates_org_period_dim",
        ),
    )
    op.create_index(
        "ix_reopen_aggregates_org_id",
        "reopen_aggregates",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_reopen_aggregates_org_id", table_name="reopen_aggregates")
    op.drop_table("reopen_aggregates")
    op.drop_index("ix_reopen_events_org_id", table_name="reopen_events")
    op.drop_table("reopen_events")
    op.drop_column("issues", "epic_key")
