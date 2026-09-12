"""Jira issue history import: jira_issues, sync_states, connection progress (issue #13).

Revision ID: 0006_jira_issue_history_import
Revises: 0005_jira_discovery
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_jira_issue_history_import"
down_revision: str | None = "0005_jira_discovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

sync_run_status = postgresql.ENUM(
    "idle",
    "running",
    "completed",
    "failed",
    name="sync_run_status",
    create_type=False,
)


def upgrade() -> None:
    sync_run_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "jira_connections",
        sa.Column("import_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "jira_connections",
        sa.Column(
            "import_imported_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.add_column(
        "jira_connections",
        sa.Column("import_total_estimate", sa.Integer(), nullable=True),
    )
    op.add_column(
        "jira_connections",
        sa.Column("import_cursor", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "jira_connections",
        sa.Column("import_updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "sync_states",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("connector", sa.String(length=64), nullable=False),
        sa.Column("sync_key", sa.String(length=128), nullable=False),
        sa.Column("cursor", sa.String(length=128), nullable=True),
        sa.Column(
            "status",
            sync_run_status,
            nullable=False,
            server_default="idle",
        ),
        sa.Column(
            "imported_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("total_estimate", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(length=500), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
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
            "connector",
            "sync_key",
            name="uq_sync_states_org_connector_key",
        ),
    )
    op.create_index("ix_sync_states_org_id", "sync_states", ["org_id"], unique=False)

    op.create_table(
        "jira_issues",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("status_name", sa.String(length=255), nullable=True),
        sa.Column("status_id", sa.String(length=64), nullable=True),
        sa.Column("issue_type_name", sa.String(length=255), nullable=True),
        sa.Column("issue_type_id", sa.String(length=64), nullable=True),
        sa.Column("raw_json", sa.Text(), nullable=True),
        sa.Column("jira_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("jira_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("node_id", sa.UUID(), nullable=True),
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
        sa.UniqueConstraint("org_id", "issue_key", name="uq_jira_issues_org_issue_key"),
        sa.UniqueConstraint(
            "org_id",
            "external_id",
            name="uq_jira_issues_org_external_id",
        ),
    )
    op.create_index("ix_jira_issues_org_id", "jira_issues", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jira_issues_org_id", table_name="jira_issues")
    op.drop_table("jira_issues")
    op.drop_index("ix_sync_states_org_id", table_name="sync_states")
    op.drop_table("sync_states")
    op.drop_column("jira_connections", "import_updated_at")
    op.drop_column("jira_connections", "import_cursor")
    op.drop_column("jira_connections", "import_total_estimate")
    op.drop_column("jira_connections", "import_imported_count")
    op.drop_column("jira_connections", "import_status")
    sync_run_status.drop(op.get_bind(), checkfirst=True)
