"""conversations and messages

Revision ID: d1dee9d9691a
Revises: 612412f057e9
Create Date: 2026-07-29 15:42:06.375185

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "d1dee9d9691a"
down_revision: str | Sequence[str] | None = "612412f057e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column(
            "id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True
        ),
        # Structured fields — parameters established, topics covered, declined /
        # unanswered, open threads — not prose. See
        # doc/components/04-conversation-memory.md.
        sa.Column("summary", JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "messages",
        sa.Column(
            "id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True
        ),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("rewritten_query", sa.Text(), nullable=True),
        sa.Column("citations", JSONB(), nullable=False, server_default="[]"),
        # Which prompt version produced this answer — what makes an answer
        # reproducible from the recorded facts alone.
        sa.Column("prompt_versions", JSONB(), nullable=False, server_default="{}"),
        sa.Column("provenance", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "provenance IS NULL OR provenance IN ('cited', 'hedged', 'declined')",
            name="ck_messages_provenance",
        ),
        # The join key into the tracing system — from a stored turn, jump
        # straight to the trace that produced it.
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # No two turns in one conversation share an index — the ordering
        # guarantee the read path relies on.
        sa.UniqueConstraint("conversation_id", "turn_index", name="uq_messages_conversation_turn"),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
