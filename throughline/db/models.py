"""ORM models.

``DevPgvectorProof`` is a disposable foundation table used only to prove
pgvector round-trips (issue #3). It is not a product embeddings table.
"""

from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from throughline.db.base import Base

# Small fixed dimension for the throwaway proof; real embeddings come later.
DEV_PGVECTOR_PROOF_DIM = 3


class DevPgvectorProof(Base):
    """Throwaway row used to verify insert/select of a pgvector column."""

    __tablename__ = "dev_pgvector_proof"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(DEV_PGVECTOR_PROOF_DIM), nullable=False)
