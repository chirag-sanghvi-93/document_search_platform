"""Re-ranking and the full search pipeline.

⚠️ Scoped to fixture collections throughout — the `chunks` table also holds 589
real corpus chunks, and an unscoped query would make these assertions depend on
data they do not control.

The cross-encoder loads once per process (~seconds warm, minutes on a cold
HuggingFace cache), so these are `integration`, not unit tests.
"""

from __future__ import annotations

import pytest

from app.engine.query.rerank import rerank, score_pairs
from app.engine.query.retrieval import SearchFilters
from app.engine.query.search import search
from app.shared.config import get_settings
from app.shared.models import OllamaClient
from app.shared.store.engine import get_session
from app.shared.types import Passage

pytestmark = pytest.mark.integration

DEFAULT = SearchFilters(collection="default")


async def _embed(text: str) -> list[float]:
    settings = get_settings()
    client = OllamaClient(settings.ollama)
    try:
        return await client.embed(text)
    finally:
        await client.aclose()


def _passage(chunk_id: str, text: str) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id="doc-1",
        source_file="fixture.pdf",
        title="Fixture",
        page=1,
        display_text=text,
        score=0.0,
    )


# ------------------------------------------------------------------- re-ranking


def test_reranker_puts_the_relevant_passage_first() -> None:
    """The precision job: fusion order is not necessarily relevance order."""
    settings = get_settings()
    # Deliberately supplied WORST-first, so passthrough would fail this.
    candidates = [
        _passage("irrelevant", "Trained service animals are carried free of charge in the cabin."),
        _passage(
            "tangential", "Wheelchair assistance must be requested 48 hours before departure."
        ),
        _passage("relevant", "Excess baggage fees apply per kilogram over the free allowance."),
    ]

    kept = rerank("what is the excess baggage fee?", candidates, settings.retrieval)

    assert kept, "expected at least the relevant passage to clear the floor"
    assert kept[0].chunk_id == "relevant"


def test_reranker_populates_scores() -> None:
    """Fusion rank was never a score; the cross-encoder score is the real one."""
    settings = get_settings()
    kept = rerank(
        "what is the excess baggage fee?",
        [_passage("relevant", "Excess baggage fees apply per kilogram over the free allowance.")],
        settings.retrieval,
    )
    assert kept
    assert kept[0].score > 0.0


def test_reranker_returns_empty_when_nothing_clears_the_floor() -> None:
    """⚠️ The behaviour that makes declining possible.

    Without a floor, the least-bad passages always come back and the
    synthesizer has no signal they are irrelevant — it will write a confident
    answer from whatever it is handed. An empty list is how the read path
    learns the corpus does not cover the question.
    """
    settings = get_settings()
    unrelated = [
        _passage("a", "Trained service animals are carried free of charge in the cabin."),
        _passage("b", "Wheelchair assistance must be requested 48 hours before departure."),
        _passage("c", "Silver tier requires 18,750 tier miles within a rolling 12-month period."),
    ]

    kept = rerank("how do I repair a bicycle derailleur?", unrelated, settings.retrieval)

    assert kept == [], (
        f"nothing should clear the floor, got {[(p.chunk_id, p.score) for p in kept]}"
    )


def test_reranker_respects_the_keep_limit() -> None:
    settings = get_settings()
    many = [
        _passage(f"c{i}", "Excess baggage fees apply per kilogram over the free allowance.")
        for i in range(10)
    ]
    kept = rerank("what is the excess baggage fee?", many, settings.retrieval, keep=3)
    assert len(kept) == 3


def test_reranker_handles_an_empty_candidate_list() -> None:
    settings = get_settings()
    assert rerank("anything", [], settings.retrieval) == []


def test_score_pairs_exposes_what_was_rejected() -> None:
    """Diagnostics need the scores of rejected candidates, not just survivors —
    "nothing cleared the floor" is far more actionable when you can see whether
    the best was 0.29 or 0.001."""
    settings = get_settings()
    scores = score_pairs(
        "what is the excess baggage fee?",
        [
            "Excess baggage fees apply per kilogram over the free allowance.",
            "Trained service animals are carried free of charge in the cabin.",
        ],
        settings.retrieval,
    )
    assert len(scores) == 2
    assert scores[0] > scores[1]


# --------------------------------------------------------------- full pipeline


async def test_search_returns_relevant_passages_with_stage_breakdown() -> None:
    settings = get_settings()
    question = "what is the excess baggage fee?"
    embedding = await _embed(question)

    with get_session() as session:
        result = search(session, question, embedding, settings.retrieval, DEFAULT)

    assert result.passages, "expected a relevant answer in the fixture corpus"
    assert not result.declined
    assert len(result.passages) <= settings.retrieval.keep_quality

    # Both halves must have contributed something inspectable — "which half
    # failed?" has to be answerable.
    assert result.vector_hits
    assert result.keyword_hits
    assert result.fused_ids

    top = result.passages[0]
    assert "excess" in top.display_text.lower() or "allowance" in top.display_text.lower()
    assert top.score > 0.0


async def test_search_declines_on_a_question_the_corpus_cannot_answer() -> None:
    """End to end: an off-topic question must produce `declined`, not the five
    least-bad airline passages."""
    settings = get_settings()
    question = "what is the best way to repair a bicycle derailleur?"
    embedding = await _embed(question)

    with get_session() as session:
        result = search(session, question, embedding, settings.retrieval, DEFAULT)

    assert result.declined
    assert result.passages == []
    # Retrieval still ran — it searched and found nothing good, which is
    # different from not searching.
    assert result.fused_ids, "fusion should still return candidates; the floor rejects them"
    assert result.rejected_by_floor > 0


async def test_search_respects_the_fast_profile_keep() -> None:
    settings = get_settings()
    question = "baggage allowance"
    embedding = await _embed(question)

    with get_session() as session:
        result = search(
            session,
            question,
            embedding,
            settings.retrieval,
            DEFAULT,
            keep=settings.retrieval.keep_fast,
        )

    assert len(result.passages) <= settings.retrieval.keep_fast
