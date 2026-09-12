"""Jira project/field discovery + per-org field mappings (issue #12).

Revision ID: 0005_jira_discovery
Revises: 0004_jira_oauth_connection
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_jira_discovery"
down_revision: str | None = "0004_jira_oauth_connection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

jira_field_concept = postgresql.ENUM(
    "acceptance_criteria",
    "story_points",
    name="jira_field_concept",
    create_type=False,
)


def upgrade() -> None:
    jira_field_concept.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "projects",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
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
        sa.UniqueConstraint("org_id", "external_id", name="uq_projects_org_external_id"),
        sa.UniqueConstraint("org_id", "key", name="uq_projects_org_key"),
    )
    op.create_index("ix_projects_org_id", "projects", ["org_id"], unique=False)

    op.create_table(
        "jira_issue_types",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("hierarchy_level", sa.Integer(), nullable=True),
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
            "external_id",
            name="uq_jira_issue_types_org_external_id",
        ),
    )
    op.create_index("ix_jira_issue_types_org_id", "jira_issue_types", ["org_id"], unique=False)

    op.create_table(
        "jira_statuses",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status_category_key", sa.String(length=64), nullable=True),
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
            "external_id",
            name="uq_jira_statuses_org_external_id",
        ),
    )
    op.create_index("ix_jira_statuses_org_id", "jira_statuses", ["org_id"], unique=False)

    op.create_table(
        "jira_field_definitions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("field_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("custom", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("schema_type", sa.String(length=128), nullable=True),
        sa.Column("schema_custom", sa.String(length=255), nullable=True),
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
            "field_id",
            name="uq_jira_field_definitions_org_field_id",
        ),
    )
    op.create_index(
        "ix_jira_field_definitions_org_id",
        "jira_field_definitions",
        ["org_id"],
        unique=False,
    )

    op.create_table(
        "jira_field_mappings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("concept", jira_field_concept, nullable=False),
        sa.Column("jira_field_id", sa.String(length=128), nullable=False),
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
        sa.UniqueConstraint("org_id", "concept", name="uq_jira_field_mappings_org_concept"),
    )
    op.create_index(
        "ix_jira_field_mappings_org_id",
        "jira_field_mappings",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jira_field_mappings_org_id", table_name="jira_field_mappings")
    op.drop_table("jira_field_mappings")
    op.drop_index("ix_jira_field_definitions_org_id", table_name="jira_field_definitions")
    op.drop_table("jira_field_definitions")
    op.drop_index("ix_jira_statuses_org_id", table_name="jira_statuses")
    op.drop_table("jira_statuses")
    op.drop_index("ix_jira_issue_types_org_id", table_name="jira_issue_types")
    op.drop_table("jira_issue_types")
    op.drop_index("ix_projects_org_id", table_name="projects")
    op.drop_table("projects")
    jira_field_concept.drop(op.get_bind(), checkfirst=True)
