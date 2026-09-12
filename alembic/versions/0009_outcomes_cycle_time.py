"""Outcomes table for stored cycle time / time-in-status (issue #17).

Revision ID: 0009_outcomes_cycle_time
Revises: 0008_canonical_normalize
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_outcomes_cycle_time"
down_revision: str | None = "0008_canonical_normalize"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outcomes",
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
        sa.Column(
            "time_in_status_seconds",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("cycle_time_seconds", sa.BigInteger(), nullable=True),
        sa.Column("pass_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cycle_passes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("first_in_progress_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transitions_fingerprint", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "external_key", name="uq_outcomes_org_external_key"),
    )
    op.create_index("ix_outcomes_org_id", "outcomes", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_outcomes_org_id", table_name="outcomes")
    op.drop_table("outcomes")
