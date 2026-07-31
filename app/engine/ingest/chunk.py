"""Chunking — Docling's HybridChunker, tokenized with the embedding model's OWN
tokenizer.

⚠️ `HybridChunker()` with no arguments silently defaults to
`sentence-transformers/all-MiniLM-L6-v2`'s tokenizer — a **different** model
than bge-m3, with a smaller context window. Left at the default, chunks would
be sized to the wrong model's token boundaries, and nothing would ever error;
it would simply chunk sub-optimally, forever. Confirmed by inspecting the
library rather than assumed: `HuggingFaceTokenizer.from_pretrained("BAAI/bge-m3",
max_tokens=768)` is what makes this match doc/components/01-docling.md's
chunk-process parameters — tokenizer = embedding model's own, max_tokens 768.

`embedding_text` is deliberately NOT assembled here. That happens after
contextualisation, once a preamble exists to fold in — see
doc/components/01-docling.md §4 and 03-contextual-agentic-rag.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from docling.chunking import HybridChunker  # type: ignore[attr-defined]
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.types.doc import DoclingDocument  # type: ignore[attr-defined]

_HEADING_SEPARATOR = " > "
_EMBEDDING_TOKENIZER_MODEL = "BAAI/bge-m3"


@dataclass(frozen=True)
class ParsedChunk:
    """Docling's output, before contextualisation. Not yet an `app.shared.types.Chunk`
    — `embedding_text` doesn't exist until a preamble is assembled onto it."""

    doc_hash: str
    position: int
    page: int
    heading_path: str
    display_text: str
    extra: dict[str, object] = field(default_factory=dict)

    @property
    def chunk_id(self) -> str:
        """Deterministic — doc_hash and position only, nothing content-derived.
        This is what lets the preamble cache and re-ingestion stay stable."""
        return f"{self.doc_hash}__{self.position:03d}"


# One tokenizer per process, per max_tokens value. Building it per document
# would reload from the HF cache on disk every time — cheap after the first
# run, but still pointless repeated work across a corpus of many documents in
# one ingestion run.
_tokenizers: dict[int, HuggingFaceTokenizer] = {}


def _tokenizer_singleton(max_tokens: int) -> HuggingFaceTokenizer:
    if max_tokens not in _tokenizers:
        _tokenizers[max_tokens] = HuggingFaceTokenizer.from_pretrained(
            _EMBEDDING_TOKENIZER_MODEL, max_tokens=max_tokens
        )
    return _tokenizers[max_tokens]


def chunk_document(
    doc_hash: str,
    docling_document: DoclingDocument,
    *,
    max_tokens: int = 768,
    min_chars: int = 100,
) -> list[ParsedChunk]:
    tokenizer = _tokenizer_singleton(max_tokens)
    chunker = HybridChunker(tokenizer=tokenizer, merge_peers=True)

    parsed: list[ParsedChunk] = []
    position = 0
    for raw_chunk in chunker.chunk(docling_document):
        text = raw_chunk.text.strip()
        if len(text) < min_chars:
            continue

        # `.meta` is typed as the abstract `BaseMeta` in docling's stubs; at
        # runtime HybridChunker always produces the concrete `DocMeta`, which is
        # where `doc_items` and `headings` actually live.
        doc_items = raw_chunk.meta.doc_items  # type: ignore[attr-defined]
        headings = raw_chunk.meta.headings  # type: ignore[attr-defined]
        page = doc_items[0].prov[0].page_no if doc_items else 0
        heading_path = _HEADING_SEPARATOR.join(headings or [])

        parsed.append(
            ParsedChunk(
                doc_hash=doc_hash,
                position=position,
                page=page,
                heading_path=heading_path,
                display_text=text,
                extra={"heading_path": heading_path},
            )
        )
        position += 1

    return parsed
