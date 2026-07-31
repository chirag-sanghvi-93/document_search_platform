"""Docling parse stage, with a cache keyed on file identity.

⚠️ OCR is explicitly disabled. `PdfPipelineOptions.do_ocr` defaults to **True** in
the installed Docling version — left alone, every ingestion would silently load
RapidOCR and spend time on it, contradicting the documented scope
(doc/components/01-docling.md marks OCR out of scope, use-case 7). Table
structure stays on; that is squarely in scope and is the extraction this system
depends on most.

The parse cache is keyed on **file hash and Docling version**, not just the file
hash — a Docling upgrade can change how a PDF is parsed, and re-using a
stale cached parse across a version bump would silently serve structure that
version no longer produces. See doc/components/01-docling.md.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling_core.types.doc import DoclingDocument  # type: ignore[attr-defined]

# Re-exported: hashing moved to app/shared/hashing.py so the API layer can use it
# without importing docling. Callers here have always used `parse.hash_file`.
from app.shared.hashing import hash_file

__all__ = ["DOCLING_VERSION", "ParsedDocument", "cache_path", "hash_file", "parse"]

DOCLING_VERSION = importlib.metadata.version("docling")


@dataclass(frozen=True)
class ParsedDocument:
    doc_hash: str
    title: str
    docling_document: DoclingDocument


def _build_converter() -> DocumentConverter:
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )


# Built once per process — the layout and table-structure models load on first
# use and stay resident, which is what keeps the second and later parses in a
# run fast. Rebuilding this per document would reload the models every time.
_converter: DocumentConverter | None = None


def _converter_singleton() -> DocumentConverter:
    global _converter
    if _converter is None:
        _converter = _build_converter()
    return _converter


def cache_path(processed_dir: Path, doc_hash: str) -> Path:
    return processed_dir / f"{doc_hash}__{DOCLING_VERSION}.json"


def parse(path: Path, processed_dir: Path, *, title_override: str | None = None) -> ParsedDocument:
    """Parse a PDF, or load its cached parse if the (hash, Docling version) pair
    has been seen before.

    Args:
        title_override: an operator-supplied title, which wins over whatever
            Docling extracts — see doc/components/11-fastapi.md §4. Extraction
            is the fallback, not the source of truth, when one was supplied.
    """
    doc_hash = hash_file(path)
    cached = cache_path(processed_dir, doc_hash)

    if cached.exists():
        docling_document = DoclingDocument.load_from_json(str(cached))
    else:
        result = _converter_singleton().convert(str(path))
        docling_document = result.document
        processed_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(docling_document.model_dump_json())

    title = title_override or docling_document.name or path.stem
    return ParsedDocument(doc_hash=doc_hash, title=title, docling_document=docling_document)
