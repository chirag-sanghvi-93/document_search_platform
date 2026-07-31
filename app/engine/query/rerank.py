"""Cross-encoder re-ranking — the precision stage.

Retrieval casts wide (20 + 20, fused to ~25-30); this narrows to the handful the
synthesizer actually sees. The two stages have different jobs, and this is the
one that decides what gets read.

**Why a cross-encoder rather than more embedding similarity.** A bi-encoder
embeds question and passage *separately*, so it never sees them together. A
cross-encoder scores the pair jointly and is markedly better at judging
relevance — at a cost that only makes sense on a few dozen candidates, which is
exactly what fusion hands it.

**The model runs in-process, not on Ollama.** It is a `sentence-transformers`
cross-encoder, a different serving pattern from the generative models. This is
why "all models are in Ollama" is not quite true — see doc/02-architecture.md §2.

**`keep_floor` is what makes declining possible.** Without it the five
least-bad passages always come back, and the synthesizer has no signal that they
are irrelevant — it will compose a confident answer from whatever it is given.
An empty result is a *meaningful* answer to a question the corpus does not cover.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from app.shared.config import RetrievalSettings
from app.shared.types import Passage

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

# One model per process. Loading takes minutes on a cold HuggingFace cache and
# ~seconds warm; doing it per request would dominate every query.
_encoder: CrossEncoder | None = None


def _load(settings: RetrievalSettings) -> CrossEncoder | None:
    """Load the cross-encoder, or return None if unavailable.

    Returning None rather than raising is deliberate: retrieval degrades to
    fusion order, which is worse but usable, instead of failing the request
    outright. The caller records that it happened — see `rerank`.
    """
    global _encoder
    if _encoder is not None:
        return _encoder

    try:
        from sentence_transformers import CrossEncoder
    except ImportError:
        logger.warning("sentence-transformers not installed; re-ranking disabled")
        return None

    try:
        _encoder = CrossEncoder(settings.reranker_model, max_length=512)
    except Exception as exc:
        logger.warning("could not load re-ranker (%s); falling back to fusion order", exc)
        return None

    return _encoder


def rank(
    question: str, passages: list[Passage], settings: RetrievalSettings
) -> tuple[list[Passage], bool]:
    """Score every candidate and sort — no floor, no truncation.

    Returns (scored, was_scored). `was_scored` is False when the cross-encoder is
    unavailable, in which case the passages come back in fusion order with no
    score: the floor is a property of cross-encoder scores, and applying it to
    fusion ranks would be meaningless.

    Split out from `rerank` so that a caller needing BOTH sides of the floor —
    what was kept and what was rejected — can have them from one scoring pass
    rather than paying for the cross-encoder twice.
    """
    if not passages:
        return [], False

    encoder = _load(settings)
    if encoder is None:
        return list(passages), False

    # `predict` types its argument as an invariant list of a very wide union
    # (text/image/audio/video), so a plain list[tuple[str, str]] does not satisfy
    # it even though pairs of strings are exactly what a text cross-encoder takes.
    pairs: list[Any] = [(question, p.display_text) for p in passages]
    scores = encoder.predict(pairs)

    # ⚠️ `replace`, not a field-by-field rebuild. The rebuild enumerated every
    # field it knew about, so adding `section` to Passage silently dropped it
    # here — retrieval carried the heading through and re-ranking threw it away,
    # leaving every citation without the line telling the reader where to look on
    # the page. Nothing failed; the field was simply absent.
    #
    # This construction only ever changes `score`, so it should only ever name
    # `score`. Any field added to Passage in future now survives by default.
    scored = [replace(p, score=float(score)) for p, score in zip(passages, scores, strict=True)]

    # Sort by score, then chunk_id — deterministic ordering even on exact ties,
    # so identical inputs cannot produce different rankings across runs.
    scored.sort(key=lambda p: (-p.score, p.chunk_id))
    return scored, True


def rerank(
    question: str,
    passages: list[Passage],
    settings: RetrievalSettings,
    *,
    keep: int | None = None,
) -> list[Passage]:
    """Rescore fused candidates against the question and keep the best.

    Args:
        keep: how many to return. Defaults to the quality profile; the fast
            profile passes `keep_fast`.

    Returns passages ordered by cross-encoder score, with `score` populated —
    the fusion rank was never a score and is not carried forward.

    ⚠️ Returns an EMPTY list when nothing clears `keep_floor`. That is a result,
    not a failure: it is how the read path learns the corpus does not cover the
    question, and it is the difference between declining and confabulating.
    """
    keep = keep if keep is not None else settings.keep_quality
    scored, was_scored = rank(question, passages, settings)

    if not was_scored:
        # Degraded: fusion order, no floor applied. The floor is a property of
        # cross-encoder scores; applying it to fusion ranks would be meaningless.
        return scored[:keep]

    return [p for p in scored if p.score >= settings.keep_floor][:keep]


def score_pairs(question: str, texts: list[str], settings: RetrievalSettings) -> list[float]:
    """Raw scores without filtering or truncation — for diagnostics and the
    evaluation harness, which need to see what was rejected and by how much."""
    encoder = _load(settings)
    if encoder is None:
        return [0.0] * len(texts)
    pairs: list[Any] = [(question, t) for t in texts]
    return [float(s) for s in encoder.predict(pairs)]


def _reset_for_tests() -> None:
    """Drop the cached encoder. Tests that exercise the degraded path need to
    force a reload; nothing in production should call this."""
    global _encoder
    _encoder = None
