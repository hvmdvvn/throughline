"""Canonical issues and transitions tables (issue #15).

Revision ID: 0008_canonical_normalize
Revises: 0007_jira_changelog_import
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_canonical_normalize"
down_revision: str | None = "0007_jira_changelog_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "issues",
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
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=255), nullable=True),
        sa.Column("issue_type", sa.String(length=255), nullable=True),
        sa.Column("acceptance_criteria", sa.Text(), nullable=True),
        sa.Column("story_points", sa.Float(), nullable=True),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "external_key", name="uq_issues_org_external_key"),
    )
    op.create_index("ix_issues_org_id", "issues", ["org_id"], unique=False)

    op.create_table(
        "issue_transitions",
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
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_status", sa.String(length=255), nullable=True),
        sa.Column("to_status", sa.String(length=255), nullable=True),
        sa.Column("actor_id", sa.String(length=128), nullable=True),
        sa.Column("actor_display_name", sa.String(length=255), nullable=True),
        sa.Column("external_event_id", sa.String(length=64), nullable=False),
        sa.Column("event_index", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "external_key",
            "external_event_id",
            "event_index",
            name="uq_issue_transitions_org_key_event_item",
        ),
    )
    op.create_index("ix_issue_transitions_org_id", "issue_transitions", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_issue_transitions_org_id", table_name="issue_transitions")
    op.drop_table("issue_transitions")
    op.drop_index("ix_issues_org_id", table_name="issues")
    op.drop_table("issues")
