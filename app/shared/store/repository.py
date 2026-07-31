"""Document, chunk and conversation persistence.

Plain SQL via SQLAlchemy Core, not an ORM. There are no declarative models
mirroring the schema — see migrations/env.py's `target_metadata = None` comment:
an ORM layer here would duplicate the schema a second time, and the two would
drift. The migrations in `migrations/versions/` are the only definition of the
schema; this module only talks to it.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.shared.types import Chunk, Citation, Document, IngestionRun, Turn


def _vector_literal(embedding: list[float]) -> str:
    """pgvector's text format — a bracketed, comma-separated literal, cast with
    `::vector` in the SQL itself. Used instead of a driver-level adapter so this
    module has no dependency beyond plain SQLAlchemy Core."""
    return "[" + ",".join(repr(x) for x in embedding) + "]"


# --------------------------------------------------------------------- ingestion runs


def create_ingestion_run(session: Session, run: IngestionRun) -> str:
    """Upsert on id, not a plain insert.

    Real callers always pass a fresh random id, so the conflict branch never
    fires for them. The fixture seed script is the exception — it deliberately
    reuses one fixed id every run, which is what makes `make seed` idempotent
    rather than failing on the second run with a primary-key violation.
    """
    row = session.execute(
        text(
            """
            INSERT INTO ingestion_runs
                (id, collection, started_at, finished_at, status, documents_seen,
                 documents_parsed, documents_skipped, chunks_created,
                 preambles_generated, config, error)
            VALUES
                (:id, :collection, :started_at, :finished_at, :status, :documents_seen,
                 :documents_parsed, :documents_skipped, :chunks_created,
                 :preambles_generated, cast(:config AS jsonb), :error)
            ON CONFLICT (id) DO UPDATE SET
                status = excluded.status,
                -- ⚠️ finished_at MUST be here. Omitting it was a real bug: the
                -- pipeline writes the run twice (once "running" at the start,
                -- once terminal at the end), so a column missing from this list
                -- silently keeps its opening value. finished_at stayed NULL on
                -- every completed run, making duration unrecoverable from the
                -- database — which defeats ingestion_runs' whole purpose as the
                -- reproducibility record.
                finished_at = excluded.finished_at,
                documents_seen = excluded.documents_seen,
                documents_parsed = excluded.documents_parsed,
                documents_skipped = excluded.documents_skipped,
                chunks_created = excluded.chunks_created,
                preambles_generated = excluded.preambles_generated,
                config = excluded.config,
                error = excluded.error
            RETURNING id
            """
        ),
        {
            "id": run.id,
            "collection": run.collection,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "status": run.status,
            "documents_seen": run.documents_seen,
            "documents_parsed": run.documents_parsed,
            "documents_skipped": run.documents_skipped,
            "chunks_created": run.chunks_created,
            "preambles_generated": run.preambles_generated,
            "config": json.dumps(run.config),
            "error": run.error,
        },
    ).one()
    return str(row.id)


# -------------------------------------------------------------------------------- documents


def write_document_with_chunks(session: Session, document: Document, chunks: list[Chunk]) -> None:
    """Delete-then-insert, as one transaction.

    Re-ingesting a changed document must remove the old version's chunks before
    inserting new ones — the cascade from `documents` makes this a single
    statement's worth of work rather than a two-step process with a window where
    the document has no chunks at all. Both statements share the caller's
    session, so a failure partway through leaves nothing committed — see
    `get_session`, which rolls back on any exception.
    """
    session.execute(
        text("DELETE FROM documents WHERE collection = :collection AND source_file = :source_file"),
        {"collection": document.collection, "source_file": document.source_file},
    )

    session.execute(
        text(
            """
            INSERT INTO documents
                (id, collection, source_file, doc_hash, title, description,
                 summary, effective_date, confidentiality, extra,
                 ingestion_run_id)
            VALUES
                (:id, :collection, :source_file, :doc_hash, :title, :description,
                 :summary, :effective_date, :confidentiality, cast(:extra AS jsonb),
                 :ingestion_run_id)
            """
        ),
        {
            "id": document.id,
            "collection": document.collection,
            "source_file": document.source_file,
            "doc_hash": document.doc_hash,
            "title": document.title,
            "description": document.description,
            "summary": document.summary,
            "effective_date": document.effective_date,
            "confidentiality": document.confidentiality,
            "extra": json.dumps(document.extra),
            "ingestion_run_id": document.ingestion_run_id,
        },
    )

    for chunk in chunks:
        session.execute(
            text(
                """
                INSERT INTO chunks
                    (id, doc_id, collection, display_text, embedding_text,
                     embedding, page, "position", extra)
                VALUES
                    (:id, :doc_id, :collection, :display_text, :embedding_text,
                     cast(:embedding AS vector), :page, :position, cast(:extra AS jsonb))
                """
            ),
            {
                "id": chunk.id,
                "doc_id": chunk.doc_id,
                "collection": chunk.collection,
                "display_text": chunk.display_text,
                "embedding_text": chunk.embedding_text,
                "embedding": _vector_literal(chunk.embedding) if chunk.embedding else None,
                "page": chunk.page,
                "position": chunk.position,
                "extra": json.dumps(chunk.extra),
            },
        )


def get_document_by_collection_and_source(
    session: Session, collection: str, source_file: str
) -> Document | None:
    row = session.execute(
        text(
            "SELECT id, collection, source_file, doc_hash, title, description, "
            "summary, effective_date, confidentiality, extra, ingestion_run_id, ingested_at "
            "FROM documents WHERE collection = :collection AND source_file = :source_file"
        ),
        {"collection": collection, "source_file": source_file},
    ).one_or_none()
    if row is None:
        return None
    return Document(
        id=str(row.id),
        collection=row.collection,
        source_file=row.source_file,
        doc_hash=row.doc_hash,
        title=row.title,
        description=row.description,
        summary=row.summary,
        effective_date=row.effective_date,
        confidentiality=row.confidentiality,
        extra=row.extra,
        ingestion_run_id=str(row.ingestion_run_id),
        ingested_at=row.ingested_at,
    )


def count_chunks(session: Session, collection: str | None = None) -> int:
    if collection is None:
        return int(session.execute(text("SELECT count(*) FROM chunks")).scalar_one())
    return int(
        session.execute(
            text("SELECT count(*) FROM chunks WHERE collection = :collection"),
            {"collection": collection},
        ).scalar_one()
    )


# ------------------------------------------------------------------------- conversations


def get_or_create_conversation(session: Session, conversation_id: str) -> str:
    existing = session.execute(
        text("SELECT id FROM conversations WHERE id = cast(:id AS uuid)"),
        {"id": conversation_id},
    ).one_or_none()
    if existing is not None:
        return str(existing.id)

    row = session.execute(
        text("INSERT INTO conversations (id) VALUES (cast(:id AS uuid)) RETURNING id"),
        {"id": conversation_id},
    ).one()
    return str(row.id)


def write_turn(session: Session, turn: Turn) -> None:
    citations_json = json.dumps(
        [
            {
                "number": c.number,
                "source_file": c.source_file,
                "title": c.title,
                "page": c.page,
                "quote": c.quote,
                "confidentiality": c.confidentiality,
                "section": c.section,
            }
            for c in turn.citations
        ]
    )
    session.execute(
        text(
            """
            INSERT INTO messages
                (conversation_id, turn_index, role, content, rewritten_query,
                 citations, prompt_versions, provenance, trace_id,
                 latency_ms, tokens_in, tokens_out)
            VALUES
                (cast(:conversation_id AS uuid), :turn_index, :role, :content,
                 :rewritten_query, cast(:citations AS jsonb),
                 cast(:prompt_versions AS jsonb), :provenance, :trace_id,
                 :latency_ms, :tokens_in, :tokens_out)
            """
        ),
        {
            "conversation_id": turn.conversation_id,
            "turn_index": turn.turn_index,
            "role": turn.role,
            "content": turn.content,
            "rewritten_query": turn.rewritten_query,
            "citations": citations_json,
            "prompt_versions": json.dumps(turn.prompt_versions),
            "provenance": turn.provenance,
            "trace_id": turn.trace_id,
            "latency_ms": turn.latency_ms,
            "tokens_in": turn.tokens_in,
            "tokens_out": turn.tokens_out,
        },
    )
    session.execute(
        text("UPDATE conversations SET last_active_at = now() WHERE id = cast(:id AS uuid)"),
        {"id": turn.conversation_id},
    )


def get_recent_turns(session: Session, conversation_id: str, limit: int) -> list[Turn]:
    """Most recent `limit` turns, returned in chronological (ascending) order —
    the shape the read path actually consumes, not the query's own descending
    scan order."""
    rows = session.execute(
        text(
            """
            SELECT id, conversation_id, turn_index, role, content, rewritten_query,
                   citations, prompt_versions, provenance, trace_id,
                   latency_ms, tokens_in, tokens_out, created_at
            FROM messages
            WHERE conversation_id = cast(:conversation_id AS uuid)
            ORDER BY turn_index DESC
            LIMIT :limit
            """
        ),
        {"conversation_id": conversation_id, "limit": limit},
    ).all()

    turns = [
        Turn(
            id=str(row.id),
            conversation_id=str(row.conversation_id),
            turn_index=row.turn_index,
            role=row.role,
            content=row.content,
            rewritten_query=row.rewritten_query,
            citations=tuple(
                Citation(
                    number=c["number"],
                    source_file=c["source_file"],
                    title=c["title"],
                    page=c["page"],
                    quote=c["quote"],
                    confidentiality=c.get("confidentiality", "internal"),
                    section=c.get("section", ""),
                )
                for c in row.citations
            ),
            prompt_versions=row.prompt_versions,
            provenance=row.provenance,
            trace_id=row.trace_id,
            latency_ms=row.latency_ms,
            tokens_in=row.tokens_in,
            tokens_out=row.tokens_out,
            created_at=row.created_at,
        )
        for row in rows
    ]
    return list(reversed(turns))


def get_conversation_summary(session: Session, conversation_id: str) -> dict[str, Any]:
    """The stored summary, or an empty mapping for a conversation with none yet.

    Returns the raw mapping rather than a typed summary so that this layer stays
    free of read-path types — `app/engine/query/memory.py` owns the shape.
    """
    row = session.execute(
        text("SELECT summary FROM conversations WHERE id = cast(:id AS uuid)"),
        {"id": conversation_id},
    ).one_or_none()
    if row is None or not isinstance(row.summary, dict):
        return {}
    return dict(row.summary)


def save_conversation_summary(session: Session, conversation_id: str, summary_json: str) -> None:
    """Replace the stored summary.

    Whole-value replacement rather than a merge in SQL: the union that protects
    against a forgetful summariser happens in `memory.merge_summaries`, where it
    can be tested without a database. Doing it in both places would mean two
    merge rules to keep in step.
    """
    session.execute(
        text(
            """
            UPDATE conversations
               SET summary = cast(:summary AS jsonb),
                   last_active_at = now()
             WHERE id = cast(:id AS uuid)
            """
        ),
        {"id": conversation_id, "summary": summary_json},
    )


def find_document_by_hash(
    session: Session, collection: str, doc_hash: str
) -> dict[str, Any] | None:
    """Whether this exact content is already indexed in this collection.

    Keyed on content, not filename: a renamed-but-unchanged file is the same
    document, and a changed file under the same name is not. This is what the
    upload endpoint checks *before* enqueueing anything.
    """
    row = session.execute(
        text(
            "SELECT id, source_file, title FROM documents "
            "WHERE collection = :collection AND doc_hash = :doc_hash"
        ),
        {"collection": collection, "doc_hash": doc_hash},
    ).one_or_none()
    if row is None:
        return None
    return {"id": str(row.id), "source_file": row.source_file, "title": row.title}


def list_documents(session: Session, collection: str | None = None) -> list[dict[str, Any]]:
    """Documents with their chunk counts, for the admin page.

    The chunk count is the operator's signal that ingestion actually produced
    something: a document row with zero chunks parsed but indexed nothing, and
    the row alone would not show it.
    """
    rows = session.execute(
        text(
            """
            SELECT d.id, d.collection, d.source_file, d.title, d.description,
                   d.effective_date, d.confidentiality, d.ingested_at,
                   count(c.id) AS chunk_count
            FROM documents d
            LEFT JOIN chunks c ON c.doc_id = d.id
            -- cast() is required, not decoration: with a bare :collection
            -- Postgres cannot infer a NULL parameter's type and raises
            -- AmbiguousParameter, so the unfiltered listing 500s.
            WHERE (cast(:collection AS text) IS NULL OR d.collection = :collection)
            GROUP BY d.id
            ORDER BY d.ingested_at DESC
            """
        ),
        {"collection": collection},
    ).all()
    return [
        {
            "id": str(row.id),
            "collection": row.collection,
            "source_file": row.source_file,
            "title": row.title,
            "description": row.description,
            "effective_date": row.effective_date.isoformat() if row.effective_date else None,
            "confidentiality": row.confidentiality,
            "ingested_at": row.ingested_at.isoformat() if row.ingested_at else None,
            "chunk_count": row.chunk_count,
        }
        for row in rows
    ]


def delete_document(session: Session, document_id: str) -> bool:
    """Remove a document. Chunks go with it via ON DELETE CASCADE."""
    row = session.execute(
        text("DELETE FROM documents WHERE id = cast(:id AS uuid) RETURNING id"),
        {"id": document_id},
    ).one_or_none()
    return row is not None


def get_ingestion_run(session: Session, run_id: str) -> dict[str, Any] | None:
    """One run's progress — what the admin page polls.

    Read from Postgres rather than from a Celery result backend, which is why
    none is configured: two accounts of the same job would eventually disagree.
    """
    row = session.execute(
        text(
            """
            SELECT id, collection, started_at, finished_at, status, documents_seen,
                   documents_parsed, documents_skipped, chunks_created,
                   preambles_generated, error
            FROM ingestion_runs WHERE id = :id
            """
        ),
        {"id": run_id},
    ).one_or_none()
    if row is None:
        return None
    return {
        "id": str(row.id),
        "collection": row.collection,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "status": row.status,
        "documents_seen": row.documents_seen,
        "documents_parsed": row.documents_parsed,
        "documents_skipped": row.documents_skipped,
        "chunks_created": row.chunks_created,
        "preambles_generated": row.preambles_generated,
        "error": row.error,
    }
