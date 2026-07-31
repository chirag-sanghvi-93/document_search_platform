"""Retrieval without generation — the whole read-side pipeline up to, but not
including, any language model writing prose.

This is what `POST /search` exposes and what the evaluation harness calls for
retrieval-only metrics. Keeping it callable on its own matters: it is how
retrieval quality gets inspected without the synthesizer's wording in the way,
and how a bad answer is attributed to *retrieval failed* versus *retrieval was
fine and generation went wrong*.

See doc/components/11-fastapi.md §3 (diagnostics) and doc/02-architecture.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.engine.query.rerank import rank
from app.engine.query.retrieval import (
    RankedChunk,
    SearchFilters,
    keyword_search,
    load_passages,
    reciprocal_rank_fusion,
    vector_search,
)
from app.shared.config import RetrievalSettings
from app.shared.types import Passage


@dataclass(frozen=True)
class SearchResult:
    """A search, with its intermediate stages preserved.

    The per-half breakdowns are not decoration: when retrieval goes wrong the
    first question is *which half failed*, and an opaque final list cannot
    answer it. Requirement 3.2 asks for retrieval to be visible.
    """

    passages: list[Passage]
    vector_hits: list[RankedChunk] = field(default_factory=list)
    keyword_hits: list[RankedChunk] = field(default_factory=list)
    fused_ids: list[str] = field(default_factory=list)
    #: Candidates that survived fusion but were rejected by the score floor.
    #: Empty is normal; a large number here means the corpus was searched and
    #: genuinely had nothing relevant.
    rejected_by_floor: int = 0

    #: ⚠️ The rejected candidates themselves, best-scoring first — not just the
    #: count above.
    #:
    #: These are what a declined answer offers as "closest matches, which do not
    #: answer the question". Keeping only the count made that feature dead code
    #: in the case it exists for: when the floor rejects EVERYTHING, `passages`
    #: is empty, and a bare refusal has nothing to suggest as a next step.
    #:
    #: They are never sources and must never be rendered as such.
    near_misses: list[Passage] = field(default_factory=list)

    @property
    def declined(self) -> bool:
        """True when retrieval found nothing above the floor.

        The read path reads this to decide whether to answer at all. It is a
        legitimate outcome, not an error — see rerank.py on why an empty result
        is the difference between declining and confabulating.
        """
        return not self.passages


def search(
    session: Session,
    question: str,
    query_embedding: list[float],
    settings: RetrievalSettings,
    filters: SearchFilters | None = None,
    *,
    keep: int | None = None,
) -> SearchResult:
    """Both halves → fuse → hydrate → re-rank → floor.

    Args:
        keep: passages to return. Defaults to `keep_quality`; the fast profile
            passes `keep_fast`.
    """
    vector_hits = vector_search(session, query_embedding, settings, filters)
    keyword_hits = keyword_search(session, question, settings, filters)

    fused_ids = reciprocal_rank_fusion([vector_hits, keyword_hits], settings)
    candidates = load_passages(session, fused_ids)

    # One scoring pass, both sides of the floor. `rerank` would give only the
    # kept side, and scoring twice to recover the other is the cost this avoids.
    keep = keep if keep is not None else settings.keep_quality
    scored, was_scored = rank(question, candidates, settings)

    if was_scored:
        kept = [p for p in scored if p.score >= settings.keep_floor][:keep]
        near = [p for p in scored if p.score < settings.keep_floor]
    else:
        kept = scored[:keep]
        near = []

    return SearchResult(
        passages=kept,
        vector_hits=vector_hits,
        keyword_hits=keyword_hits,
        fused_ids=fused_ids,
        rejected_by_floor=len(near),
        near_misses=near,
    )
