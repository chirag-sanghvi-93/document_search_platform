"""Repository layer.

GIVEN  the repository layer
WHEN   a document and its chunks are written inside one transaction that
       then raises
THEN   neither the document nor any chunk is present
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.shared.store.engine import get_session
from app.shared.store.repository import (
    create_ingestion_run,
    get_document_by_collection_and_source,
    get_or_create_conversation,
    get_recent_turns,
    write_document_with_chunks,
    write_turn,
)
from app.shared.types import Chunk, Citation, Document, IngestionRun, Turn

pytestmark = pytest.mark.integration


@pytest.fixture
def ingestion_run_id() -> Iterator[str]:
    with get_session() as session:
        run_id = create_ingestion_run(
            session,
            IngestionRun(
                id=str(uuid4()),
                collection="test",
                started_at=datetime.now(UTC),
                status="completed",
            ),
        )

    yield run_id

    # ⚠️ Without this, rows written here outlive the test and pollute the shared
    # dev database for whatever runs next — exactly what happened to
    # tests/integration/test_seed.py's unscoped counts before this cleanup
    # existed: leftover documents/chunks from this file inflated its totals when
    # the two ran together in the full suite, though each passed standalone.
    with get_session() as session:
        session.execute(text("DELETE FROM documents WHERE collection = 'test'"))
        session.execute(
            text("DELETE FROM ingestion_runs WHERE id = cast(:id AS uuid)"), {"id": run_id}
        )


def test_write_then_read_round_trip(ingestion_run_id: str) -> None:
    doc_id = str(uuid4())
    document = Document(
        id=doc_id,
        collection="test",
        source_file="round-trip.pdf",
        doc_hash="hash-a",
        title="Round Trip Doc",
        ingestion_run_id=ingestion_run_id,
    )
    chunks = [
        Chunk(
            id="rt-chunk-0",
            doc_id=doc_id,
            collection="test",
            display_text="the actual clause",
            embedding_text="context: the actual clause",
            page=1,
            position=0,
        )
    ]

    with get_session() as session:
        write_document_with_chunks(session, document, chunks)

    with get_session() as session:
        fetched = get_document_by_collection_and_source(session, "test", "round-trip.pdf")

    assert fetched is not None
    assert fetched.title == "Round Trip Doc"
    assert fetched.doc_hash == "hash-a"


def test_reingesting_the_same_source_replaces_old_chunks(ingestion_run_id: str) -> None:
    """Delete-then-insert: a document re-ingested under the same (collection,
    source_file) must end up with ONLY the new chunks — the cascade from
    `documents` is what makes this atomic rather than a two-step process with a
    window where stale and fresh chunks coexist."""
    doc_id_v1 = str(uuid4())
    v1 = Document(
        id=doc_id_v1,
        collection="test",
        source_file="reingest.pdf",
        doc_hash="hash-v1",
        title="Version 1",
        ingestion_run_id=ingestion_run_id,
    )
    v1_chunks = [
        Chunk(
            id="reingest-chunk-old",
            doc_id=doc_id_v1,
            collection="test",
            display_text="old content",
            embedding_text="old content",
            page=1,
            position=0,
        )
    ]

    with get_session() as session:
        write_document_with_chunks(session, v1, v1_chunks)

    doc_id_v2 = str(uuid4())
    v2 = Document(
        id=doc_id_v2,
        collection="test",
        source_file="reingest.pdf",  # same collection + source_file
        doc_hash="hash-v2",
        title="Version 2",
        ingestion_run_id=ingestion_run_id,
    )
    v2_chunks = [
        Chunk(
            id="reingest-chunk-new",
            doc_id=doc_id_v2,
            collection="test",
            display_text="new content",
            embedding_text="new content",
            page=1,
            position=0,
        )
    ]

    with get_session() as session:
        write_document_with_chunks(session, v2, v2_chunks)

    with get_session() as session:
        old_chunk = session.execute(
            text("SELECT id FROM chunks WHERE id = 'reingest-chunk-old'")
        ).one_or_none()
        new_chunk = session.execute(
            text("SELECT id FROM chunks WHERE id = 'reingest-chunk-new'")
        ).one_or_none()
        fetched = get_document_by_collection_and_source(session, "test", "reingest.pdf")

    assert old_chunk is None, "the superseded chunk must not survive re-ingestion"
    assert new_chunk is not None
    assert fetched is not None
    assert fetched.doc_hash == "hash-v2"


def test_a_failure_partway_through_leaves_nothing_committed(ingestion_run_id: str) -> None:
    """The acceptance criterion this whole function exists to prove: document
    and chunks share one transaction, so a mid-write failure rolls back both."""
    doc_id = str(uuid4())
    document = Document(
        id=doc_id,
        collection="test",
        source_file="atomicity.pdf",
        doc_hash="hash-atomic",
        title="Should Not Persist",
        ingestion_run_id=ingestion_run_id,
    )
    # A chunk with a doc_id that doesn't match `document.id` violates the FK
    # constraint on chunks.doc_id, forcing a failure inside the same statement
    # batch as the document insert.
    broken_chunks = [
        Chunk(
            id="atomicity-chunk",
            doc_id=str(uuid4()),  # does not reference `document.id` at all
            collection="test",
            display_text="never persisted",
            embedding_text="never persisted",
            page=1,
            position=0,
        )
    ]

    with pytest.raises(DBAPIError), get_session() as session:
        write_document_with_chunks(session, document, broken_chunks)

    with get_session() as session:
        fetched = get_document_by_collection_and_source(session, "test", "atomicity.pdf")
        orphan_chunk = session.execute(
            text("SELECT id FROM chunks WHERE id = 'atomicity-chunk'")
        ).one_or_none()

    assert fetched is None, "the document must not persist when its chunks failed to write"
    assert orphan_chunk is None


def test_conversation_and_turn_round_trip() -> None:
    conversation_id = str(uuid4())
    with get_session() as session:
        get_or_create_conversation(session, conversation_id)
        write_turn(
            session,
            Turn(
                id=str(uuid4()),
                conversation_id=conversation_id,
                turn_index=0,
                role="user",
                content="what is the checked baggage allowance?",
            ),
        )
        write_turn(
            session,
            Turn(
                id=str(uuid4()),
                conversation_id=conversation_id,
                turn_index=1,
                role="assistant",
                content="23kg in Economy [1].",
                provenance="cited",
                trace_id="trace-round-trip",
                citations=(
                    Citation(
                        number=1,
                        source_file="fixture-baggage-policy.pdf",
                        title="Baggage Policy",
                        page=3,
                        quote="23kg",
                    ),
                ),
                prompt_versions={"synthesizer": "1"},
            ),
        )

    with get_session() as session:
        turns = get_recent_turns(session, conversation_id, limit=10)

    assert [t.turn_index for t in turns] == [0, 1]  # chronological, not reversed
    assert turns[1].provenance == "cited"
    assert turns[1].trace_id == "trace-round-trip"
    assert turns[1].citations[0].source_file == "fixture-baggage-policy.pdf"


def test_get_or_create_conversation_is_idempotent() -> None:
    conversation_id = str(uuid4())
    with get_session() as session:
        first = get_or_create_conversation(session, conversation_id)
        second = get_or_create_conversation(session, conversation_id)

    assert first == second == conversation_id


def test_finished_at_persists_on_the_terminal_write() -> None:
    """⚠️ Regression: `finished_at` was missing from the upsert's DO UPDATE list.

    The pipeline writes each run twice — once as "running" at the start, once
    terminal at the end. A column absent from DO UPDATE silently keeps its
    opening value, so every completed run had finished_at = NULL and its duration
    was unrecoverable. That defeats the purpose of ingestion_runs as the
    reproducibility record, and nothing errored to reveal it.
    """
    run_id = str(uuid4())
    started = datetime.now(UTC)

    with get_session() as session:
        create_ingestion_run(
            session,
            IngestionRun(id=run_id, collection="test", started_at=started, status="running"),
        )

    finished = datetime.now(UTC)
    with get_session() as session:
        create_ingestion_run(
            session,
            IngestionRun(
                id=run_id,
                collection="test",
                started_at=started,
                finished_at=finished,
                status="completed",
                chunks_created=42,
            ),
        )

    with get_session() as session:
        row = session.execute(
            text(
                "SELECT status, finished_at, chunks_created FROM ingestion_runs "
                "WHERE id = cast(:id AS uuid)"
            ),
            {"id": run_id},
        ).one()
        session.execute(
            text("DELETE FROM ingestion_runs WHERE id = cast(:id AS uuid)"), {"id": run_id}
        )

    assert row.status == "completed"
    assert row.finished_at is not None, "finished_at did not persist on the terminal write"
    assert row.chunks_created == 42
