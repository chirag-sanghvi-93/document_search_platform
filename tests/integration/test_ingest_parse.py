"""Parse and summariser-input extraction, against real cached parses.

Marked `integration` because these read the on-disk parse cache produced by a
real Docling run. They do not need Ollama or Postgres.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_PROCESSED = Path("data/processed")


def _load_any_cached_parse() -> object:
    from docling_core.types.doc import DoclingDocument  # type: ignore[attr-defined]

    candidates = sorted(_PROCESSED.glob("*.json"))
    if not candidates:
        pytest.skip("no cached parses in data/processed — run an ingestion first")
    return DoclingDocument.load_from_json(str(candidates[0]))


def _load_table_only_parse() -> object:
    """The dangerous-goods guide: 3 tables, 2 pictures, ZERO text items."""
    from docling_core.types.doc import DoclingDocument  # type: ignore[attr-defined]

    for path in sorted(_PROCESSED.glob("*.json")):
        doc = DoclingDocument.load_from_json(str(path))
        labels = {str(getattr(item, "label", "")) for item, _ in doc.iterate_items()}
        has_tables = any("table" in label for label in labels)
        has_text = any(getattr(item, "text", None) for item, _ in doc.iterate_items())
        if has_tables and not has_text:
            return doc
    pytest.skip("no table-only parse cached — ingest the dangerous-goods guide first")


def test_summariser_input_is_not_empty_for_a_table_only_document() -> None:
    """⚠️ The regression this exists to prevent.

    A Docling table item has NO `.text` attribute. A naive
    `getattr(item, "text")` filter therefore skips tables entirely, and a
    table-only document hands the summariser an empty string — at which point
    the model infers a summary from the FILENAME. It produced a confident,
    accurate-sounding summary that way, which is precisely what made the bug
    hard to spot.

    Because the document summary feeds every chunk's contextual preamble, an
    empty summariser input silently degrades retrieval across the whole
    document.
    """
    from app.engine.ingest.pipeline import _first_pages_text

    doc = _load_table_only_parse()
    extracted = _first_pages_text(doc)  # type: ignore[arg-type]

    assert extracted.strip(), "table-only document yielded no summariser input"
    assert len(extracted) > 500, f"suspiciously little content: {len(extracted)} chars"


def test_summariser_input_excludes_picture_placeholders() -> None:
    """Pictures are out of scope (OCR disabled) and their markdown
    serialisation is a placeholder telling you to enable image generation —
    noise in a summariser prompt, not content."""
    from app.engine.ingest.pipeline import _first_pages_text

    doc = _load_any_cached_parse()
    extracted = _first_pages_text(doc)  # type: ignore[arg-type]

    assert "Image not available" not in extracted
    assert "generate_picture_images" not in extracted


def test_summariser_input_is_bounded() -> None:
    """A 69-page document's 'first two pages' is unbounded without a cap, and a
    table-dense page serialises to thousands of characters."""
    from app.engine.ingest.pipeline import _SUMMARY_INPUT_MAX_CHARS, _first_pages_text

    doc = _load_table_only_parse()
    extracted = _first_pages_text(doc)  # type: ignore[arg-type]

    assert len(extracted) <= _SUMMARY_INPUT_MAX_CHARS + 1


def test_parse_cache_filename_encodes_docling_version() -> None:
    """The cache key is (file hash, Docling version) — a Docling upgrade can
    change how a PDF parses, so a stale parse must not be reused across one."""
    from app.engine.ingest.parse import DOCLING_VERSION, cache_path

    path = cache_path(Path("/tmp/whatever"), "abc123")
    assert path.name == f"abc123__{DOCLING_VERSION}.json"


def test_chunk_id_is_deterministic() -> None:
    """Derived from doc_hash and position only — nothing content-derived, so
    the preamble cache and re-ingestion stay stable."""
    from app.engine.ingest.chunk import ParsedChunk

    a = ParsedChunk(doc_hash="h", position=7, page=1, heading_path="", display_text="x")
    b = ParsedChunk(doc_hash="h", position=7, page=99, heading_path="different", display_text="y")

    assert a.chunk_id == b.chunk_id == "h__007"
