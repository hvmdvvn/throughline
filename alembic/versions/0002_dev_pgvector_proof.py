"""Create disposable dev_pgvector_proof table.

Revision ID: 0002_dev_pgvector_proof
Revises: 0001_enable_pgvector
Create Date: 2026-09-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0002_dev_pgvector_proof"
down_revision: str | None = "0001_enable_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match throughline.db.models.DEV_PGVECTOR_PROOF_DIM
_PROOF_DIM = 3


def upgrade() -> None:
    op.create_table(
        "dev_pgvector_proof",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("embedding", Vector(_PROOF_DIM), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("dev_pgvector_proof")
