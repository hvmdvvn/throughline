"""Diagnostic onboarding sessions (issue #27).

Revision ID: 0015_diagnostic_onboarding
Revises: 0014_diagnostic_reports
Create Date: 2026-09-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_diagnostic_onboarding"
down_revision: str | None = "0014_diagnostic_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "diagnostic_onboardings",
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
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("notify_email", sa.String(length=320), nullable=False),
        sa.Column("range_start", sa.Date(), nullable=False),
        sa.Column("range_end", sa.Date(), nullable=False),
        sa.Column("jql", sa.Text(), nullable=True),
        sa.Column("progress_status", sa.String(length=32), nullable=True),
        sa.Column("progress_imported_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_total_estimate", sa.Integer(), nullable=True),
        sa.Column("progress_detail", sa.String(length=500), nullable=True),
        sa.Column("progress_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("orchestrator_job_id", sa.String(length=128), nullable=True),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email_status", sa.String(length=32), nullable=True),
        sa.Column("email_detail", sa.String(length=500), nullable=True),
        sa.Column("email_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_stage", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["diagnostic_reports.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", name="uq_diagnostic_onboardings_org"),
    )
    op.create_index(
        "ix_diagnostic_onboardings_org_id",
        "diagnostic_onboardings",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_diagnostic_onboardings_org_id", table_name="diagnostic_onboardings")
    op.drop_table("diagnostic_onboardings")
