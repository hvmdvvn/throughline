"""Create jira_connections table for Atlassian OAuth 3LO (issue #10).

Revision ID: 0004_jira_oauth_connection
Revises: 0003_tenancy_models
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_jira_oauth_connection"
down_revision: str | None = "0003_tenancy_models"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

jira_connection_status = postgresql.ENUM(
    "connected",
    "disconnected",
    "error",
    name="jira_connection_status",
    create_type=False,
)


def upgrade() -> None:
    jira_connection_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "jira_connections",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("cloud_id", sa.String(length=128), nullable=True),
        sa.Column("site_url", sa.String(length=512), nullable=True),
        sa.Column("site_name", sa.String(length=255), nullable=True),
        sa.Column("encrypted_access_token", sa.LargeBinary(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column(
            "status",
            jira_connection_status,
            nullable=False,
            server_default="disconnected",
        ),
        sa.Column("status_detail", sa.String(length=500), nullable=True),
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
        sa.UniqueConstraint("org_id", name="uq_jira_connections_org"),
    )
    op.create_index("ix_jira_connections_org_id", "jira_connections", ["org_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jira_connections_org_id", table_name="jira_connections")
    op.drop_table("jira_connections")
    jira_connection_status.drop(op.get_bind(), checkfirst=True)
