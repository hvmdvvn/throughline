"""Estimation accuracy metrics: estimate vs cycle time (issue #21).

Revision ID: 0013_estimation_accuracy
Revises: 0012_spec_quality_indicators
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_estimation_accuracy"
down_revision: str | None = "0012_spec_quality_indicators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "issues",
        sa.Column("original_estimate_seconds", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "estimation_accuracy_issue_metrics",
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
        sa.Column("team_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("story_points", sa.Float(), nullable=True),
        sa.Column("original_estimate_seconds", sa.BigInteger(), nullable=True),
        sa.Column("cycle_time_seconds", sa.BigInteger(), nullable=True),
        sa.Column(
            "has_usable_estimate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "estimate_kind",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
        sa.Column("estimated_seconds", sa.Float(), nullable=True),
        sa.Column("accuracy_ratio", sa.Float(), nullable=True),
        sa.Column("accuracy_bucket", sa.String(length=32), nullable=False),
        sa.Column("seconds_per_point", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "external_key",
            name="uq_estimation_accuracy_issue_metrics_org_key",
        ),
    )
    op.create_index(
        "ix_estimation_accuracy_issue_metrics_org_id",
        "estimation_accuracy_issue_metrics",
        ["org_id"],
        unique=False,
    )

    op.create_table(
        "estimation_accuracy_aggregates",
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
        sa.Column(
            "period_grain",
            sa.String(length=16),
            nullable=False,
            server_default="month",
        ),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column(
            "dimension_key",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "with_estimate_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "comparable_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("coverage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("over_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("under_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accurate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "accuracy_ratio_sum",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "evidence_issue_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "period_start",
            "period_grain",
            "dimension",
            "dimension_key",
            name="uq_estimation_accuracy_aggregates_org_period_dim",
        ),
    )
    op.create_index(
        "ix_estimation_accuracy_aggregates_org_id",
        "estimation_accuracy_aggregates",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_estimation_accuracy_aggregates_org_id",
        table_name="estimation_accuracy_aggregates",
    )
    op.drop_table("estimation_accuracy_aggregates")
    op.drop_index(
        "ix_estimation_accuracy_issue_metrics_org_id",
        table_name="estimation_accuracy_issue_metrics",
    )
    op.drop_table("estimation_accuracy_issue_metrics")
    op.drop_column("issues", "original_estimate_seconds")
