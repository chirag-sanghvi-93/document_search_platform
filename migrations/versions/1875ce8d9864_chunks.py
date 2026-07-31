"""chunks

Revision ID: 1875ce8d9864
Revises: 08ec5ab1ec62
Create Date: 2026-07-29 15:35:52.513869

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision: str = "1875ce8d9864"
down_revision: str | Sequence[str] | None = "08ec5ab1ec62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Fixed to bge-m3's output width — see doc/components/06-ollama.md (the authority
# on model selection) and doc/components/02b-pgvector-postgresql.md §7, "the
# dimension lock". Changing embedding models means dropping and recreating this
# column and re-embedding the corpus — the contextual preamble cache (keyed on
# chunk TEXT, not this column) is what keeps that a coffee break, not a migration
# ordeal. Written here as a fixed literal, not read from settings, because a
# migration must be reproducible regardless of what the running config happens
# to be at apply time.
EMBEDDING_DIMENSION = 1024


def upgrade() -> None:
    op.create_table(
        "chunks",
        # Deterministic — derived in application code from doc_hash + position,
        # never server-generated. This is what lets the preamble cache and the
        # fixture corpus stay reproducible across runs.
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "doc_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collection", sa.Text(), nullable=False),
        # Two columns, never conflated. display_text is quoted in citations;
        # embedding_text (heading path + contextual preamble + display_text) is
        # what becomes the vector and the keyword index. Rendering embedding_text
        # under a page citation would show words that do not appear on that page.
        sa.Column("display_text", sa.Text(), nullable=False),
        sa.Column("embedding_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSION), nullable=True),
        # Generated, not maintained by application code — a GENERATED column
        # cannot drift from embedding_text the way a separately-written one could.
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('english', embedding_text)", persisted=True),
            nullable=True,
        ),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("extra", JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_table("chunks")
