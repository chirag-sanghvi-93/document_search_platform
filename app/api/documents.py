"""Corpus curation: upload, list, delete, and run progress.

⚠️ Every route here translates HTTP and calls an ordinary function. No route
contains engine logic — that is what keeps the CLI and the upload path two
callers of one pipeline rather than two implementations.

See doc/components/11-fastapi.md §4.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse

from app.shared.config import get_settings
from app.shared.hashing import hash_file
from app.shared.store import repository
from app.shared.store.engine import get_session
from app.shared.types import IngestionRun

logger = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])


def _queue(collection: str, source_file: str, run_id: str, metadata: dict[str, Any]) -> None:
    """Enqueue, or fail the request.

    ⚠️ A broker that cannot be reached must produce a 503, never a 202. A 202
    says "accepted, work will happen" — returning one for a message that was
    never queued leaves an operator watching a run that will never start, with
    nothing anywhere saying why.
    """
    from app.tasks.ingestion import ingest_document

    try:
        ingest_document.apply_async(args=[collection, source_file, run_id, metadata])
    except Exception as exc:  # broker unreachable, connection refused, timeout
        logger.error("could not enqueue ingestion for %s: %s", source_file, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ingestion queue unavailable; the document was not accepted",
        ) from exc


@router.post("/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    collection: str = Form(...),
    title: str | None = Form(None),
    description: str | None = Form(None),
    effective_date: date | None = Form(None),
    confidentiality: Literal["public", "internal", "confidential"] | None = Form(None),
) -> dict[str, Any]:
    """Accept a PDF and queue it for ingestion.

    ⚠️ Only `file` and `collection` are required. Every metadata field is
    optional and, when supplied, overrides what ingestion would generate — the
    operator knows what the document is; the summariser only infers it.

    `description` is the highest-value field of the lot: it feeds the corpus
    description the planner uses to decide whether a question is in scope at all.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "a .pdf file is required")

    settings = get_settings()
    raw_dir = settings.ingestion.raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Land it somewhere temporary first: hashing decides whether this file should
    # exist in the corpus directory at all, and a duplicate that has already been
    # written there has overwritten the original it duplicates.
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as staged:
        shutil.copyfileobj(file.file, staged)
        staged_path = Path(staged.name)

    try:
        # ⚠️ Synchronous, before enqueueing anything. Hashing a PDF costs well
        # under a second, and it is the one piece of work that decides whether
        # there is any work — queueing first would mean paying for a full parse
        # to discover the document was already indexed.
        doc_hash = hash_file(staged_path)

        with get_session() as session:
            existing = repository.find_document_by_hash(session, collection, doc_hash)
            if existing is not None:
                response.status_code = status.HTTP_200_OK
                return {
                    "status": "duplicate",
                    "document_id": existing["id"],
                    "source_file": existing["source_file"],
                    "detail": "identical content is already indexed in this collection",
                }

        destination = raw_dir / Path(file.filename).name
        shutil.move(str(staged_path), destination)

        run_id = str(uuid4())
        metadata: dict[str, Any] = {
            key: value
            for key, value in (
                ("title", title),
                ("description", description),
                ("effective_date", effective_date.isoformat() if effective_date else None),
                ("confidentiality", confidentiality),
            )
            if value is not None
        }

        # The run record is created HERE, not in the worker, so the 202 can carry
        # an id the caller can poll immediately. A worker that has not been
        # scheduled yet would otherwise leave that id pointing at nothing.
        with get_session() as session:
            repository.create_ingestion_run(
                session,
                IngestionRun(
                    id=run_id,
                    collection=collection,
                    started_at=datetime.now(UTC),
                    status="running",
                    config={"source_file": destination.name, "uploaded": True},
                ),
            )

        _queue(collection, destination.name, run_id, metadata)

        return {
            "status": "accepted",
            "document_id": None,  # assigned by ingestion; poll the run
            "ingestion_run_id": run_id,
            "source_file": destination.name,
            "collection": collection,
        }
    finally:
        staged_path.unlink(missing_ok=True)


@router.get("/documents")
async def list_documents(collection: str | None = None) -> dict[str, Any]:
    with get_session() as session:
        documents = repository.list_documents(session, collection)
    return {"documents": documents, "count": len(documents)}


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: str) -> Response:
    """Remove a document and, by cascade, its chunks."""
    with get_session() as session:
        removed = repository.delete_document(session, document_id)
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such document")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ingestion-runs/{run_id}")
async def get_ingestion_run(run_id: str) -> dict[str, Any]:
    """Progress for one run — what the admin page polls."""
    with get_session() as session:
        run = repository.get_ingestion_run(session, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such ingestion run")
    return run


@router.get("/admin", include_in_schema=False)
async def admin_page() -> FileResponse:
    """The operator surface.

    A single self-contained file with no external requests — see the comment at
    the top of admin.html for why that is a requirement rather than a preference.
    """
    page = Path(__file__).resolve().parents[1] / "static" / "admin.html"
    return FileResponse(page, media_type="text/html")
