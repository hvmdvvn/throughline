"""Jira changelog import: status transitions + sibling sync cursor (issue #14).

Revision ID: 0007_jira_changelog_import
Revises: 0006_jira_issue_history_import
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_jira_changelog_import"
down_revision: str | None = "0006_jira_issue_history_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jira_issues",
        sa.Column("changelog_imported_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "jira_connections",
        sa.Column("changelog_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "jira_connections",
        sa.Column(
            "changelog_imported_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "jira_connections",
        sa.Column("changelog_total_estimate", sa.Integer(), nullable=True),
    )
    op.add_column(
        "jira_connections",
        sa.Column("changelog_cursor", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "jira_connections",
        sa.Column("changelog_updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "jira_status_transitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=False),
        sa.Column("history_id", sa.String(length=64), nullable=False),
        sa.Column("item_index", sa.Integer(), nullable=False),
        sa.Column("transitioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_account_id", sa.String(length=128), nullable=True),
        sa.Column("actor_display_name", sa.String(length=255), nullable=True),
        sa.Column("from_status_id", sa.String(length=64), nullable=True),
        sa.Column("from_status_name", sa.String(length=255), nullable=True),
        sa.Column("to_status_id", sa.String(length=64), nullable=True),
        sa.Column("to_status_name", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "issue_key",
            "history_id",
            "item_index",
            name="uq_jira_status_transitions_org_issue_history_item",
        ),
    )
    op.create_index(
        "ix_jira_status_transitions_org_id",
        "jira_status_transitions",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        "ix_jira_status_transitions_org_issue_key",
        "jira_status_transitions",
        ["org_id", "issue_key"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_jira_status_transitions_org_issue_key",
        table_name="jira_status_transitions",
    )
    op.drop_index("ix_jira_status_transitions_org_id", table_name="jira_status_transitions")
    op.drop_table("jira_status_transitions")
    op.drop_column("jira_connections", "changelog_updated_at")
    op.drop_column("jira_connections", "changelog_cursor")
    op.drop_column("jira_connections", "changelog_total_estimate")
    op.drop_column("jira_connections", "changelog_imported_count")
    op.drop_column("jira_connections", "changelog_status")
    op.drop_column("jira_issues", "changelog_imported_at")
