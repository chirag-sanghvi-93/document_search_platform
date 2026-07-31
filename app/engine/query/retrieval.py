"""Retrieval: two searches, fused by rank, then re-ranked.

Everything between "here is a question" and "here are the best passages for it".
No agents, no query rewriting, no generation — those belong to the read path that
calls this. See doc/components/02-llamaindex.md and
doc/components/02b-pgvector-postgresql.md §6.

Three things here are load-bearing and easy to get silently wrong:

**Filters go INSIDE both queries.** An approximate index picks its best *k* by
distance first; filtering that result afterwards returns fewer rows than asked
for — not an error, just quietly short. Both halves also build their filters from
one shared helper, because if the two drift they are searching different corpora
and nothing reports it.

**Keyword search uses OR semantics, never `plainto_tsquery`.** That function
ANDs every term, so a fifteen-word natural-language question matches nothing.
Vector search still returns results, so the system looks fine while half of it is
dead.

**Fusion combines ranks, not scores.** Cosine distance and `ts_rank` are on
incomparable scales, and `ts_rank` shifts with query length — so weighted score
arithmetic is meaningless. RRF sidesteps the problem entirely, and the
cross-encoder rescores everything downstream anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.shared.config import RetrievalSettings
from app.shared.types import Passage


@dataclass(frozen=True)
class SearchFilters:
    """Restrictions applied identically to both search halves."""

    collection: str | None = None
    source_files: tuple[str, ...] = ()
    #: Matched against the chunk's `extra` JSONB — corpus-specific fields the
    #: engine itself knows nothing about.
    extra_equals: dict[str, str] = field(default_factory=dict)

    def to_sql(self, params: dict[str, Any]) -> str:
        """Render as SQL fragments, mutating `params` with the bound values.

        Returns a string starting with " AND ..." or empty. One implementation,
        called by both halves — see the module docstring on filter drift.
        """
        clauses: list[str] = []

        if self.collection is not None:
            clauses.append("c.collection = :f_collection")
            params["f_collection"] = self.collection

        if self.source_files:
            clauses.append("d.source_file = ANY(:f_source_files)")
            params["f_source_files"] = list(self.source_files)

        for i, (key, value) in enumerate(sorted(self.extra_equals.items())):
            clauses.append(f"c.extra ->> :f_extra_key_{i} = :f_extra_val_{i}")
            params[f"f_extra_key_{i}"] = key
            params[f"f_extra_val_{i}"] = value

        return "".join(f" AND {clause}" for clause in clauses)


@dataclass(frozen=True)
class RankedChunk:
    """A chunk with its position in one search half's result list.

    Rank, not score — RRF consumes positions, and keeping the raw score here
    would invite someone to try arithmetic on values from different scales.
    """

    chunk_id: str
    rank: int


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(x) for x in embedding) + "]"


def vector_search(
    session: Session,
    query_embedding: list[float],
    settings: RetrievalSettings,
    filters: SearchFilters | None = None,
) -> list[RankedChunk]:
    """Semantic half. Cosine distance via `<=>`, matching the index's
    `vector_cosine_ops` operator class — a mismatch here silently disables the
    index and falls back to a sequential scan."""
    filters = filters or SearchFilters()
    params: dict[str, Any] = {
        "embedding": _vector_literal(query_embedding),
        "limit": settings.vector_k,
    }
    filter_sql = filters.to_sql(params)

    rows = session.execute(
        text(
            f"""
            SELECT c.id
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE c.embedding IS NOT NULL{filter_sql}
            ORDER BY c.embedding <=> cast(:embedding AS vector)
            LIMIT :limit
            """
        ),
        params,
    ).all()

    return [RankedChunk(chunk_id=row.id, rank=i + 1) for i, row in enumerate(rows)]


def _to_or_tsquery(question: str) -> str:
    """Build an OR-joined tsquery from a natural-language question.

    ⚠️ Deliberately not `plainto_tsquery`, which ANDs every term — fatal for a
    long question, and silent. Rare terms (clause numbers, route codes) still
    rank highly through `ts_rank` precisely because they are rare, which is this
    half's entire purpose.
    """
    tokens = [t for t in ("".join(ch if ch.isalnum() else " " for ch in question)).split() if t]
    return " | ".join(tokens)


def keyword_search(
    session: Session,
    question: str,
    settings: RetrievalSettings,
    filters: SearchFilters | None = None,
) -> list[RankedChunk]:
    """Keyword half. Catches exact strings that carry little semantic weight —
    `4.2`, `ABZ`, `EY360A` — which embeddings largely miss."""
    filters = filters or SearchFilters()
    tsquery = _to_or_tsquery(question)
    if not tsquery:
        return []

    params: dict[str, Any] = {"tsquery": tsquery, "limit": settings.keyword_k}
    filter_sql = filters.to_sql(params)

    rows = session.execute(
        text(
            f"""
            SELECT c.id
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id,
                 to_tsquery('english', :tsquery) AS q
            WHERE c.tsv @@ q{filter_sql}
            ORDER BY ts_rank(c.tsv, q) DESC
            LIMIT :limit
            """
        ),
        params,
    ).all()

    return [RankedChunk(chunk_id=row.id, rank=i + 1) for i, row in enumerate(rows)]


def reciprocal_rank_fusion(
    halves: list[list[RankedChunk]],
    settings: RetrievalSettings,
) -> list[str]:
    """Fuse ranked lists into one ordered, de-duplicated list of chunk ids.

        score(chunk) = Σ 1 / (k + rank_in_list)

    Appearing in both lists beats ranking well in only one — agreement between
    two different retrieval methods is a strong signal, and that is the whole
    reason to fuse rather than pick.
    """
    scores: dict[str, float] = {}
    for half in halves:
        for entry in half:
            scores[entry.chunk_id] = scores.get(entry.chunk_id, 0.0) + 1.0 / (
                settings.rrf_k + entry.rank
            )

    # Tie-break on chunk_id so ordering is deterministic rather than
    # dict-insertion dependent — otherwise identical inputs could produce
    # different orders across runs, and no test could pin it down.
    return [cid for cid, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]


def load_passages(
    session: Session,
    chunk_ids: list[str],
    *,
    drop_superseded: bool = True,
) -> list[Passage]:
    """Hydrate fused ids into full passages, preserving the given order.

    Postgres returns rows in whatever order it likes, so the fused ranking is
    re-imposed here rather than trusted from the query.

    Args:
        drop_superseded: when two passages carry identical `display_text`, keep
            only the one from the most recent `effective_date`. See
            `_drop_superseded_duplicates` for why this matters.
    """
    if not chunk_ids:
        return []

    rows = session.execute(
        text(
            """
            SELECT c.id, c.doc_id, d.source_file, d.title, c.page,
                   c.display_text, d.confidentiality, d.effective_date,
                   -- Carried for citations: tells the reader where to look ON
                   -- the page. Not part of the citation's identity, which is
                   -- (file, page) — see doc/components/05-citation-handling.md §5.
                   c.extra ->> 'heading_path' AS heading_path
            FROM chunks c
            JOIN documents d ON d.id = c.doc_id
            WHERE c.id = ANY(:ids)
            """
        ),
        {"ids": chunk_ids},
    ).all()

    by_id = {
        row.id: (
            Passage(
                chunk_id=row.id,
                doc_id=str(row.doc_id),
                source_file=row.source_file,
                title=row.title,
                page=row.page,
                display_text=row.display_text,
                score=0.0,  # set by the re-ranker; fusion rank is not a score
                confidentiality=row.confidentiality,
                section=row.heading_path or "",
            ),
            row.effective_date,
        )
        for row in rows
    }
    ordered = [by_id[cid] for cid in chunk_ids if cid in by_id]

    if drop_superseded:
        ordered = _drop_superseded_duplicates(ordered)

    return [passage for passage, _ in ordered]


def _drop_superseded_duplicates(
    ordered: list[tuple[Passage, date | None]],
) -> list[tuple[Passage, date | None]]:
    """Collapse identical passage text down to its most recent version.

    A corpus can hold several versions of the same document — ours holds current
    and previous conditions of carriage, **50 of whose chunks are byte-identical**.
    Both versions retrieve, both score identically, and two of five result slots
    then carry one slot's worth of information. With `keep=5` that is 40% of what
    the synthesizer sees, spent on a duplicate.

    Deduplicating on text alone would be wrong — it would pick arbitrarily
    between versions. `effective_date` is what makes the choice principled:
    where the text is identical, the newest document wins.

    ⚠️ **A NULL `effective_date` loses to any real date, and ties break on
    retrieval order.** Nothing populates the column automatically, so a corpus
    that never supplies it behaves exactly as before — first-retrieved wins —
    rather than silently reordering on a field nobody filled in.
    """
    best: dict[str, int] = {}
    result: list[tuple[Passage, date | None]] = []

    for passage, effective in ordered:
        key = passage.display_text
        if key not in best:
            best[key] = len(result)
            result.append((passage, effective))
            continue

        _, incumbent_date = result[best[key]]
        if _supersedes(effective, incumbent_date):
            result[best[key]] = (passage, effective)

    return result


def _supersedes(candidate: date | None, incumbent: date | None) -> bool:
    """True when `candidate` is a strictly newer version than `incumbent`.

    NULL is treated as "unknown", never as "newest": an undated document cannot
    displace a dated one, and two undated documents keep retrieval order.
    """
    if candidate is None:
        return False
    if incumbent is None:
        return True
    return candidate > incumbent


def hybrid_search(
    session: Session,
    question: str,
    query_embedding: list[float],
    settings: RetrievalSettings,
    filters: SearchFilters | None = None,
) -> list[Passage]:
    """Both halves, fused, hydrated — everything up to but excluding re-ranking.

    Kept separate from re-ranking so the fusion step is inspectable on its own,
    and so `POST /search` can show what each half contributed.
    """
    vector_half = vector_search(session, query_embedding, settings, filters)
    keyword_half = keyword_search(session, question, settings, filters)
    fused_ids = reciprocal_rank_fusion([vector_half, keyword_half], settings)
    return load_passages(session, fused_ids)
