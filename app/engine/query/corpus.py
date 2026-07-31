"""Corpus description — what the planner is told the corpus contains.

Without this, out-of-scope classification is inference from the question alone.
That works for "what is the weather in Abu Dhabi" and fails for anything whose
answerability depends on what the corpus *happens* to hold: "what is the
surfboard fee" is only out of scope if sports equipment is not covered, and
nothing in the question reveals that.

Assembled from each document's `description` (operator-supplied) falling back to
`summary` (generated) — the same authority order as everywhere else. Cached and
invalidated on corpus change rather than rebuilt per request: it is identical for
every question asked against a given corpus.

See doc/components/11-fastapi.md §4 and doc/components/07-crewai.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class CorpusDescription:
    collection: str
    text: str
    document_count: int

    @property
    def is_empty(self) -> bool:
        return self.document_count == 0


#: (collection -> (description, cached_at)). Small, bounded by collection count.
_cache: dict[str, tuple[CorpusDescription, float]] = {}

#: The corpus changes only on ingestion, so a long TTL is right. Short enough
#: that a freshly ingested document becomes visible to the planner without a
#: restart; long enough that it is not rebuilt per request.
CACHE_TTL_S = 300

#: Cap on the assembled text. It is paid for on EVERY planner call, so an
#: unbounded description would tax every question in proportion to corpus size.
MAX_CHARS = 2_000


def _assemble(session: Session, collection: str) -> CorpusDescription:
    rows = session.execute(
        text(
            """
            SELECT title,
                   -- operator-supplied description wins; generated summary is
                   -- the fallback. Same authority order as ingestion.
                   coalesce(nullif(description, ''), nullif(summary, '')) AS blurb
            FROM documents
            WHERE collection = :collection
            ORDER BY title
            """
        ),
        {"collection": collection},
    ).all()

    lines: list[str] = []
    budget = MAX_CHARS
    for row in rows:
        if not row.blurb:
            continue
        # One line per document, trimmed — the planner needs the shape of the
        # corpus, not the whole of every summary.
        blurb = " ".join(str(row.blurb).split())
        entry = f"  · {blurb}"
        if len(entry) > budget:
            break
        lines.append(entry)
        budget -= len(entry)

    body = "This corpus covers:\n" + "\n".join(lines) if lines else ""
    return CorpusDescription(collection=collection, text=body, document_count=len(rows))


def get_corpus_description(
    session: Session, collection: str, *, force_refresh: bool = False
) -> CorpusDescription:
    now = time.monotonic()
    if not force_refresh:
        cached = _cache.get(collection)
        if cached is not None and (now - cached[1]) < CACHE_TTL_S:
            return cached[0]

    description = _assemble(session, collection)
    _cache[collection] = (description, now)
    return description


def invalidate(collection: str | None = None) -> None:
    """Drop the cache. Called after ingestion, which is the only thing that
    changes what a corpus covers."""
    if collection is None:
        _cache.clear()
    else:
        _cache.pop(collection, None)
