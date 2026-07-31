"""Recall measurement — what the approximate index is losing.

HNSW is an *approximate* nearest-neighbour index. It trades exactness for speed,
and nothing reports what it dropped: a query returns five plausible passages with
no indication a better sixth existed and was missed. That silence is the problem
this module exists to break.

**Method.** Run the same query twice — once through the index, once with index
scans disabled so Postgres falls back to a sequential scan, which is exact by
construction. The exact result is ground truth; recall is the overlap.

**Why this matters more here than in a typical search system.** The design casts
wide and lets the cross-encoder do the precision work — but the re-ranker can
only rescore what retrieval handed it. A candidate the index missed is
unrecoverable, and invisible.

See doc/components/02b-pgvector-postgresql.md §5, "Measure the recall".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.engine.query.retrieval import SearchFilters, _vector_literal
from app.shared.config import RetrievalSettings


@dataclass(frozen=True)
class RecallResult:
    question: str
    k: int
    ef_search: int
    approximate_ids: list[str]
    exact_ids: list[str]

    @property
    def overlap(self) -> int:
        return len(set(self.approximate_ids) & set(self.exact_ids))

    @property
    def recall(self) -> float:
        """Fraction of the true top-k that the index actually returned."""
        if not self.exact_ids:
            return 1.0
        return self.overlap / len(self.exact_ids)

    @property
    def missed_ids(self) -> list[str]:
        """Chunks the exact search found and the index did not — the ones that
        would have been silently unavailable to the re-ranker."""
        approximate = set(self.approximate_ids)
        return [cid for cid in self.exact_ids if cid not in approximate]


def _search_ids(
    session: Session,
    query_embedding: list[float],
    k: int,
    filters: SearchFilters,
    *,
    exact: bool,
    ef_search: int | None = None,
) -> list[str]:
    params: dict[str, Any] = {"embedding": _vector_literal(query_embedding), "limit": k}
    filter_sql = filters.to_sql(params)

    # ⚠️ RESET FIRST. `SET LOCAL` lasts for the whole transaction, and both the
    # approximate and exact queries run on the SAME session — so without this,
    # the second call inherits the first's planner settings.
    #
    # That was a real bug, and a silent one: the "exact" query inherited
    # `enable_seqscan = off` and `hnsw.ef_search = 2` from the approximate run,
    # so both sides executed the *same degraded index path* and recall was
    # trivially 1.000 at every setting. The tell was `exact` returning only 4
    # rows for a k of 20 — a genuine sequential scan over 589 chunks cannot
    # return 4.
    session.execute(text("RESET enable_seqscan"))
    session.execute(text("RESET enable_indexscan"))
    session.execute(text("RESET enable_bitmapscan"))
    session.execute(text("RESET hnsw.ef_search"))

    if exact:
        # Force a sequential scan. This is the ground truth: every vector is
        # compared, so the top-k is exact by construction.
        session.execute(text("SET LOCAL enable_indexscan = off"))
        session.execute(text("SET LOCAL enable_bitmapscan = off"))
    else:
        # ⚠️ Forcing the index path is REQUIRED, not an optimisation.
        #
        # At corpus scale (589 chunks) the planner correctly judges a sequential
        # scan cheaper and ignores the HNSW index — so without this, BOTH sides
        # of the comparison run exact search and recall is trivially 1.000 at
        # every ef_search, including absurdly low ones. That is the measurement
        # measuring itself, which is worse than no measurement: it looks like
        # proof the index is lossless.
        #
        # Verified with EXPLAIN: without this the plan is `Seq Scan on chunks`;
        # with it, `Index Scan using ix_chunks_embedding_hnsw`.
        session.execute(text("SET LOCAL enable_seqscan = off"))
        if ef_search is not None:
            # LOCAL so it reverts with the transaction — a sweep must not leave
            # the connection tuned differently than it found it.
            session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))

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
    return [row.id for row in rows]


def measure_recall(
    session: Session,
    question: str,
    query_embedding: list[float],
    settings: RetrievalSettings,
    filters: SearchFilters | None = None,
    *,
    k: int | None = None,
    ef_search: int | None = None,
) -> RecallResult:
    """Compare indexed retrieval against exact search for one query."""
    filters = filters or SearchFilters()
    k = k if k is not None else settings.vector_k

    approximate = _search_ids(
        session, query_embedding, k, filters, exact=False, ef_search=ef_search
    )
    exact = _search_ids(session, query_embedding, k, filters, exact=True)

    return RecallResult(
        question=question,
        k=k,
        ef_search=ef_search or 0,
        approximate_ids=approximate,
        exact_ids=exact,
    )


def mean_recall(results: list[RecallResult]) -> float:
    if not results:
        return 1.0
    return sum(r.recall for r in results) / len(results)
