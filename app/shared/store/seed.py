"""Load the deterministic fixture corpus.

    make seed

Idempotent — delete-then-insert per document means re-running produces the
identical 25 chunks, not a second copy. Never calls Ollama: the embeddings were
generated once (`tests/fixtures/generate_embeddings.py`) and are committed at
`tests/fixtures/chunk_embeddings.json`, so this can run on a machine that has
never pulled bge-m3.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.shared.store.engine import get_session
from app.shared.store.repository import create_ingestion_run, write_document_with_chunks
from app.shared.types import Chunk, Document, IngestionRun
from tests.fixtures.fixture_chunks import CHUNKS, DOCUMENTS

EMBEDDINGS_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "chunk_embeddings.json"
)

# Fixed, not random — re-seeding must produce the identical run id, matching the
# "deterministic and idempotent" requirement on this whole corpus.
FIXTURE_RUN_ID = "00000000-0000-0000-0000-0000000feed0"


def load_embeddings() -> dict[str, list[float]]:
    if not EMBEDDINGS_PATH.exists():
        raise FileNotFoundError(
            f"{EMBEDDINGS_PATH} is missing. Generate it once with "
            "`uv run python -m tests.fixtures.generate_embeddings` (requires Ollama), "
            "then commit the file — seeding itself must never need a model."
        )
    data: dict[str, list[float]] = json.loads(EMBEDDINGS_PATH.read_text())
    return data


def seed() -> int:
    embeddings = load_embeddings()
    missing = [c.chunk_id for c in CHUNKS if c.chunk_id not in embeddings]
    if missing:
        raise RuntimeError(f"no committed embedding for: {missing}")

    with get_session() as session:
        create_ingestion_run(
            session,
            IngestionRun(
                id=FIXTURE_RUN_ID,
                collection="fixtures",
                started_at=datetime.now(UTC),
                status="completed",
                config={"source": "tests/fixtures/fixture_chunks.py"},
            ),
        )

        chunks_written = 0
        for doc in DOCUMENTS:
            doc_chunks = [c for c in CHUNKS if c.doc_hash == doc.doc_hash]

            document = Document(
                id=str(uuid4()),
                collection=doc.collection,
                source_file=doc.source_file,
                doc_hash=doc.doc_hash,
                title=doc.title,
                description=doc.description,
                ingestion_run_id=FIXTURE_RUN_ID,
            )

            chunk_records = [
                Chunk(
                    id=fc.chunk_id,
                    doc_id=document.id,
                    collection=doc.collection,
                    display_text=fc.display_text,
                    embedding_text=fc.embedding_text,
                    page=fc.page,
                    position=fc.position,
                    embedding=embeddings[fc.chunk_id],
                    extra={"heading_path": fc.heading_path},
                )
                for fc in doc_chunks
            ]

            write_document_with_chunks(session, document, chunk_records)
            chunks_written += len(chunk_records)

    return chunks_written


def main() -> None:
    count = seed()
    print(f"seeded {len(DOCUMENTS)} documents, {count} chunks")


if __name__ == "__main__":
    main()
