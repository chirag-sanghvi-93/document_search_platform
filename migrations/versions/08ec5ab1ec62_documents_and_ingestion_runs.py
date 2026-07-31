"""documents and ingestion_runs

Revision ID: 08ec5ab1ec62
Revises:
Create Date: 2026-07-29 15:31:11.012279

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "08ec5ab1ec62"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # CHECK, not a Postgres ENUM type: adding a status later is an ALTER
        # CONSTRAINT, not the more disruptive ALTER TYPE ... ADD VALUE.
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name="ck_ingestion_runs_status"
        ),
        sa.Column("documents_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_parsed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("documents_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("preambles_generated", sa.Integer(), nullable=False, server_default="0"),
        # The record that turns "what changed?" into a lookup, not archaeology —
        # see doc/components/02b-pgvector-postgresql.md §4.
        sa.Column("config", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
    )

    op.create_table(
        "documents",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("source_file", sa.Text(), nullable=False),
        sa.Column("doc_hash", sa.Text(), nullable=False),
        # Operator-supplied wins; extracted-at-parse is the fallback; the filename
        # is the fallback to that — see doc/components/11-fastapi.md §4.
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("confidentiality", sa.Text(), nullable=False, server_default="internal"),
        sa.CheckConstraint(
            "confidentiality IN ('public', 'internal', 'confidential')",
            name="ck_documents_confidentiality",
        ),
        # Everything corpus-specific that isn't read by the engine goes here —
        # not a column per field, which is how the schema stops working for the
        # next document set.
        sa.Column("extra", sa.dialects.postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "ingestion_run_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_runs.id"),
            nullable=False,
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # What makes re-ingestion detection possible: the same file re-uploaded
        # to the same collection is found, not silently duplicated.
        sa.UniqueConstraint(
            "collection", "source_file", name="uq_documents_collection_source_file"
        ),
    )


def downgrade() -> None:
    op.drop_table("documents")
    op.drop_table("ingestion_runs")
