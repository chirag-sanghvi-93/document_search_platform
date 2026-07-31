"""indexes

Revision ID: 612412f057e9
Revises: 1875ce8d9864
Create Date: 2026-07-29 15:38:28.047436

"""

from collections.abc import Sequence

from alembic import op

revision: str = "612412f057e9"
down_revision: str | Sequence[str] | None = "1875ce8d9864"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # HNSW, not IVFFlat: accepts inserts without degrading and needs no training
    # pass. m/ef_construction are Postgres defaults; recall matters more than
    # build speed here — see doc/components/02b-pgvector-postgresql.md §5.
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw
        ON chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # ⚠️ The database-level default below is a SAFETY NET, not the only place this
    # is set. ef_search is a session-scoped GUC — if the querying code forgets to
    # set it, the connection silently falls back to Postgres's default of 40,
    # which is too close to a k of 20 and quietly costs recall. Setting it here
    # means a forgotten SET degrades gracefully to 100, not 40.
    op.execute("ALTER DATABASE rag SET hnsw.ef_search = 100")

    # GIN for the keyword half. Not covered by the vector index above — two
    # separate indexes for two separate searches, fused in the application.
    op.execute("CREATE INDEX ix_chunks_tsv_gin ON chunks USING gin (tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_tsv_gin")
    op.execute("ALTER DATABASE rag RESET hnsw.ef_search")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
