"""Spec quality indicators: AC/description proxies + aggregates (issue #20).

Revision ID: 0012_spec_quality_indicators
Revises: 0011_scope_change_metrics
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_spec_quality_indicators"
down_revision: str | None = "0011_scope_change_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("issues", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("issues", sa.Column("team_key", sa.String(length=64), nullable=True))

    op.create_table(
        "spec_quality_issue_indicators",
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
        sa.Column("team_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("ac_coverage", sa.String(length=32), nullable=False),
        sa.Column(
            "description_length_chars",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("description_length_bucket", sa.String(length=16), nullable=False),
        sa.Column("comment_traffic_status", sa.String(length=64), nullable=False),
        sa.Column("comment_traffic_count", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_ref", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "external_key",
            name="uq_spec_quality_issue_indicators_org_key",
        ),
    )
    op.create_index(
        "ix_spec_quality_issue_indicators_org_id",
        "spec_quality_issue_indicators",
        ["org_id"],
        unique=False,
    )

    op.create_table(
        "spec_quality_aggregates",
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
        sa.Column("issue_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ac_present_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ac_missing_or_empty_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "ac_mapping_unavailable_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "description_empty_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "description_short_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "description_long_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "description_length_chars_sum",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "comment_traffic_unavailable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
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
            name="uq_spec_quality_aggregates_org_period_dim",
        ),
    )
    op.create_index(
        "ix_spec_quality_aggregates_org_id",
        "spec_quality_aggregates",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_spec_quality_aggregates_org_id", table_name="spec_quality_aggregates")
    op.drop_table("spec_quality_aggregates")
    op.drop_index(
        "ix_spec_quality_issue_indicators_org_id",
        table_name="spec_quality_issue_indicators",
    )
    op.drop_table("spec_quality_issue_indicators")
    op.drop_column("issues", "team_key")
    op.drop_column("issues", "description")
