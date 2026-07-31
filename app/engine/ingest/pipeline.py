"""The ingestion pipeline: hash → parse → summarise → chunk → contextualise →
embed → write, one run at a time.

⚠️ Stages are batched by model, never interleaved per chunk. All document
summaries first, then all chunk preambles, then all embeddings — across the
WHOLE run, not per document. Interleaving would swap models in and out of
memory thousands of times, turning a twenty-minute run into a fourteen-hour
one, with correct output throughout and nothing to signal the difference. See
doc/02-architecture.md §4.

The write path does NOT fail forward. A half-ingested document is worse than an
absent one — searchable, incomplete, and indistinguishable from a complete one.
A failure marks the run `failed`; whatever documents already completed their
own transaction in phase 4 remain indexed, and nothing after the failure point
does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from docling_core.types.doc import DocItemLabel, DoclingDocument  # type: ignore[attr-defined]

from app.engine.ingest.chunk import ParsedChunk, chunk_document
from app.engine.ingest.contextualise import assemble_embedding_text, contextualise_chunk
from app.engine.ingest.index import embed_texts
from app.engine.ingest.parse import ParsedDocument, hash_file, parse
from app.engine.ingest.summarise import summarise_document
from app.shared.config import Settings
from app.shared.models import OllamaClient
from app.shared.prompts import PromptRegistry, PromptSet
from app.shared.store.engine import get_session
from app.shared.store.repository import (
    create_ingestion_run,
    get_document_by_collection_and_source,
    write_document_with_chunks,
)
from app.shared.types import Chunk, Confidentiality, Document, IngestionRun, IngestionStatus


class DocumentMetadata(TypedDict, total=False):
    """Operator-supplied fields — the CLI's equivalent of the upload endpoint's
    form fields, since there is no HTTP request here to carry them. See
    doc/components/11-fastapi.md §4: every field here is optional, and supplied
    always overrides generated."""

    title: str
    description: str
    effective_date: date
    confidentiality: Confidentiality
    extra: dict[str, object]


@dataclass
class _WorkingChunk:
    parsed: ParsedChunk
    preamble: str = ""
    embedding_text: str = ""
    embedding: list[float] = field(default_factory=list)


@dataclass
class _WorkingDocument:
    path: Path
    doc_hash: str
    title: str
    summary: str | None
    description: str | None
    metadata: DocumentMetadata
    chunks: list[_WorkingChunk]


def _heading_tree(docling_document: DoclingDocument) -> str:
    headings: list[str] = []
    for item, _level in docling_document.iterate_items():
        label = getattr(item, "label", None)
        text = getattr(item, "text", None)
        if label in (DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER) and text:
            headings.append(str(text))
    return "\n".join(headings)


#: Character budget for the summariser's view of a document's opening pages.
#: A table-dense page can serialise to several thousand characters, and a
#: 69-page document's "first two pages" is unbounded without this. Sized to stay
#: well inside num_ctx (8192 tokens) once the prompt template is added.
_SUMMARY_INPUT_MAX_CHARS = 12_000


def _first_pages_text(docling_document: DoclingDocument, max_page: int = 2) -> str:
    """Text the summariser sees, INCLUDING serialised tables.

    ⚠️ Tables must be handled separately: a Docling table item has **no `.text`
    attribute at all**, so a naive `getattr(item, "text")` filter skips them
    silently. That is not a cosmetic omission — a table-only document (our
    dangerous-goods guide is 3 tables and 2 pictures, with zero text items)
    would hand the summariser an empty string, and the model would then infer a
    summary from the FILENAME alone. It produced a plausible, accurate-sounding
    summary that way, which is exactly what made the bug hard to notice: the
    output looked right because the filename happened to be descriptive.

    Since the document summary is the input to *every* chunk's contextual
    preamble, a filename-derived summary silently degrades retrieval for the
    whole document.
    """
    parts: list[str] = []
    budget = _SUMMARY_INPUT_MAX_CHARS

    for item, _level in docling_document.iterate_items():
        prov = getattr(item, "prov", None)
        if not prov or prov[0].page_no > max_page:
            continue

        # Pictures are out of scope (OCR is disabled), and their markdown
        # serialisation is a placeholder comment — "Image not available, please
        # use generate_picture_images" — which is pure noise in a summariser
        # prompt.
        if getattr(item, "label", None) == DocItemLabel.PICTURE:
            continue

        text = getattr(item, "text", None)
        if text:
            fragment = str(text)
        elif hasattr(item, "export_to_markdown"):
            # Tables, primarily. Serialised so their content reaches the
            # summariser rather than being dropped for lacking `.text`.
            fragment = str(item.export_to_markdown(docling_document))
        else:
            continue

        if not fragment.strip():
            continue

        parts.append(fragment[:budget])
        budget -= len(fragment)
        if budget <= 0:
            break

    return "\n".join(parts)


async def _parse_summarise_chunk(
    path: Path,
    doc_hash: str,
    meta: DocumentMetadata,
    settings: Settings,
    prompts: PromptSet,
) -> _WorkingDocument:
    parsed_doc: ParsedDocument = parse(
        path, settings.ingestion.processed_dir, title_override=meta.get("title")
    )

    description = meta.get("description")
    summary = await summarise_document(
        title=parsed_doc.title,
        heading_tree=_heading_tree(parsed_doc.docling_document),
        first_pages_text=_first_pages_text(parsed_doc.docling_document),
        description=description,
        prompts=prompts,
        ollama_settings=settings.ollama,
    )

    raw_chunks = chunk_document(
        doc_hash,
        parsed_doc.docling_document,
        max_tokens=settings.ingestion.max_tokens_per_chunk,
        min_chars=settings.ingestion.min_chunk_chars,
    )

    return _WorkingDocument(
        path=path,
        doc_hash=doc_hash,
        title=parsed_doc.title,
        summary=None if description else summary,
        description=description,
        metadata=meta,
        chunks=[_WorkingChunk(parsed=c) for c in raw_chunks],
    )


async def ingest_collection(
    settings: Settings,
    collection: str,
    raw_dir: Path,
    *,
    document_metadata: dict[str, DocumentMetadata] | None = None,
    only: list[str] | None = None,
    run_id: str | None = None,
) -> IngestionRun:
    """Ingest every PDF in `raw_dir` into `collection`.

    Args:
        only: restrict to these filenames. The upload path passes the one file
            just received — without it, every upload would re-ingest the whole
            corpus, which is hours of model work to index one document.
        run_id: use this id instead of generating one. The upload endpoint
            creates the run record synchronously so it can return the id in a
            202, then hands the SAME id to the worker; otherwise the caller
            would be polling a run that never gets written.
    """
    document_metadata = document_metadata or {}
    run_id = run_id or str(uuid4())
    started_at = datetime.now(UTC)

    prompts = PromptRegistry(settings.phoenix).resolve_all()
    ollama = OllamaClient(settings.ollama)

    documents_seen = 0
    documents_skipped = 0
    documents_parsed = 0
    preambles_generated = 0
    chunks_created = 0
    error: str | None = None
    status: IngestionStatus = "running"

    with get_session() as session:
        create_ingestion_run(
            session,
            IngestionRun(
                id=run_id,
                collection=collection,
                started_at=started_at,
                status="running",
                config={
                    "raw_dir": str(raw_dir),
                    "small_model": settings.ollama.small_model,
                    "embedding_model": settings.ollama.embedding_model,
                    "max_tokens_per_chunk": settings.ingestion.max_tokens_per_chunk,
                },
            ),
        )

    try:
        pdf_paths = sorted(raw_dir.glob("*.pdf"))
        if only is not None:
            wanted = set(only)
            pdf_paths = [p for p in pdf_paths if p.name in wanted]
        pending: list[tuple[Path, str]] = []

        with get_session() as session:
            for path in pdf_paths:
                documents_seen += 1
                doc_hash = hash_file(path)
                existing = get_document_by_collection_and_source(session, collection, path.name)
                if existing is not None and existing.doc_hash == doc_hash:
                    documents_skipped += 1
                    continue
                pending.append((path, doc_hash))

        # -- phase 1: parse + summarise + chunk, for every pending document ----
        working_docs: list[_WorkingDocument] = []
        for path, doc_hash in pending:
            meta = document_metadata.get(path.name, {})
            wdoc = await _parse_summarise_chunk(path, doc_hash, meta, settings, prompts)
            working_docs.append(wdoc)
            documents_parsed += 1

        # -- phase 2: contextualise EVERY chunk across EVERY document ----------
        for wdoc in working_docs:
            for wchunk in wdoc.chunks:
                wchunk.preamble = await contextualise_chunk(
                    document_summary=wdoc.summary or wdoc.description or "",
                    heading_path=wchunk.parsed.heading_path,
                    chunk_text=wchunk.parsed.display_text,
                    prompts=prompts,
                    ollama_settings=settings.ollama,
                    preamble_dir=settings.ingestion.preamble_dir,
                )
                wchunk.embedding_text = assemble_embedding_text(
                    heading_path=wchunk.parsed.heading_path,
                    preamble=wchunk.preamble,
                    display_text=wchunk.parsed.display_text,
                )
                preambles_generated += 1

        # -- phase 3: embed EVERY chunk across EVERY document ------------------
        all_texts = [wc.embedding_text for wdoc in working_docs for wc in wdoc.chunks]
        all_embeddings = await embed_texts(all_texts, ollama)
        cursor = 0
        for wdoc in working_docs:
            for wchunk in wdoc.chunks:
                wchunk.embedding = all_embeddings[cursor]
                cursor += 1

        # -- phase 4: write, one document (one transaction) at a time ----------
        with get_session() as session:
            for wdoc in working_docs:
                document = Document(
                    id=str(uuid4()),
                    collection=collection,
                    source_file=wdoc.path.name,
                    doc_hash=wdoc.doc_hash,
                    title=wdoc.title,
                    description=wdoc.description,
                    summary=wdoc.summary,
                    effective_date=wdoc.metadata.get("effective_date"),
                    confidentiality=wdoc.metadata.get("confidentiality", "internal"),
                    extra=wdoc.metadata.get("extra", {}),
                    ingestion_run_id=run_id,
                )
                chunk_records = [
                    Chunk(
                        id=wc.parsed.chunk_id,
                        doc_id=document.id,
                        collection=collection,
                        display_text=wc.parsed.display_text,
                        embedding_text=wc.embedding_text,
                        page=wc.parsed.page,
                        position=wc.parsed.position,
                        embedding=wc.embedding,
                        extra=wc.parsed.extra,
                    )
                    for wc in wdoc.chunks
                ]
                write_document_with_chunks(session, document, chunk_records)
                chunks_created += len(chunk_records)

        status = "completed"

    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        raise

    finally:
        await ollama.aclose()
        with get_session() as session:
            create_ingestion_run(
                session,
                IngestionRun(
                    id=run_id,
                    collection=collection,
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                    status=status,
                    documents_seen=documents_seen,
                    documents_parsed=documents_parsed,
                    documents_skipped=documents_skipped,
                    chunks_created=chunks_created,
                    preambles_generated=preambles_generated,
                    config={"raw_dir": str(raw_dir)},
                    error=error,
                ),
            )

    return IngestionRun(
        id=run_id,
        collection=collection,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        status="completed",
        documents_seen=documents_seen,
        documents_parsed=documents_parsed,
        documents_skipped=documents_skipped,
        chunks_created=chunks_created,
        preambles_generated=preambles_generated,
    )
