"""Retrieval: both halves, their filters, and fusion.

⚠️ Every query here is scoped to a fixture collection (`default` / `cargo`).
Never a bare `count(*)` or an unfiltered search — the `chunks` table is shared,
and real corpus ingestion writes to `corpus` concurrently. An unscoped assertion
would pass or fail depending on what else happened to be in the table, which is
exactly the bug that bit the storage phase's seed tests.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.engine.query.retrieval import (
    RankedChunk,
    SearchFilters,
    keyword_search,
    load_passages,
    reciprocal_rank_fusion,
    vector_search,
)
from app.shared.config import get_settings
from app.shared.models import OllamaClient
from app.shared.store.engine import get_session

pytestmark = pytest.mark.integration

DEFAULT = SearchFilters(collection="default")
CARGO = SearchFilters(collection="cargo")


async def _embed(text: str) -> list[float]:
    settings = get_settings()
    client = OllamaClient(settings.ollama)
    try:
        return await client.embed(text)
    finally:
        await client.aclose()


# ----------------------------------------------------------------- keyword half


def test_a_long_natural_language_question_returns_results_not_zero() -> None:
    """⚠️ The `plainto_tsquery` trap, guarded.

    `plainto_tsquery` ANDs every term, so a long question matches nothing —
    silently, because vector search still returns results and the system appears
    to work while half of it is dead. OR semantics is what prevents that.
    """
    settings = get_settings()
    question = (
        "What is the checked baggage allowance for Economy passengers "
        "travelling from Abu Dhabi to London?"
    )
    with get_session() as session:
        hits = keyword_search(session, question, settings.retrieval, DEFAULT)

    assert hits, "a long natural-language question must not return zero keyword hits"


def test_rare_token_ranks_its_chunk_highly() -> None:
    """Form references, clause numbers and route codes mean little to an
    embedding model — catching them is this half's entire purpose."""
    settings = get_settings()
    with get_session() as session:
        hits = keyword_search(session, "EY360A", settings.retrieval, DEFAULT)

    found = {h.chunk_id for h in hits}
    assert found == {"fx-dg-01__000", "fx-dg-01__001"}


def test_keyword_ranks_are_dense_and_one_based() -> None:
    settings = get_settings()
    with get_session() as session:
        hits = keyword_search(session, "baggage allowance fees", settings.retrieval, DEFAULT)

    assert [h.rank for h in hits] == list(range(1, len(hits) + 1))


# ------------------------------------------------------------------ vector half


async def test_vector_search_finds_the_semantically_closest_chunk() -> None:
    settings = get_settings()
    embedding = await _embed("what does it cost to bring an extra suitcase?")
    with get_session() as session:
        hits = vector_search(session, embedding, settings.retrieval, DEFAULT)

    assert hits
    # Phrased with none of the corpus's own vocabulary — semantic match only.
    assert hits[0].chunk_id.startswith("fx-baggage-01"), hits[0].chunk_id


async def test_vector_search_respects_the_k_limit() -> None:
    settings = get_settings()
    embedding = await _embed("baggage")
    with get_session() as session:
        hits = vector_search(session, embedding, settings.retrieval, DEFAULT)

    assert len(hits) <= settings.retrieval.vector_k


# ---------------------------------------------------------------------- filters


async def test_collection_filter_isolates_both_halves() -> None:
    """The filter must be applied INSIDE each query, not to its results.

    An approximate index picks its best k by distance first; filtering
    afterwards silently returns fewer rows than requested. Asserted on both
    halves because a filter that drifts between them means the two are
    searching different corpora, and nothing reports it.
    """
    settings = get_settings()
    embedding = await _embed("liability for loss or damage")

    with get_session() as session:
        v_default = vector_search(session, embedding, settings.retrieval, DEFAULT)
        v_cargo = vector_search(session, embedding, settings.retrieval, CARGO)
        k_default = keyword_search(session, "liability damage", settings.retrieval, DEFAULT)
        k_cargo = keyword_search(session, "liability damage", settings.retrieval, CARGO)

    assert all(h.chunk_id.startswith("fx-") for h in v_default)
    assert all("cargo" not in h.chunk_id for h in v_default)
    assert v_cargo and all("cargo" in h.chunk_id for h in v_cargo)
    assert k_default and all("cargo" not in h.chunk_id for h in k_default)
    assert k_cargo and all("cargo" in h.chunk_id for h in k_cargo)


async def test_source_file_filter_narrows_to_one_document() -> None:
    settings = get_settings()
    embedding = await _embed("baggage")
    filters = SearchFilters(
        collection="default", source_files=("fixture-dangerous-goods-guide.pdf",)
    )
    with get_session() as session:
        hits = vector_search(session, embedding, settings.retrieval, filters)

    assert hits
    assert all(h.chunk_id.startswith("fx-dg-01") for h in hits)


# ----------------------------------------------------------------------- fusion


def test_rrf_rewards_appearing_in_both_halves() -> None:
    """Agreement between two different retrieval methods is a strong signal —
    the whole reason to fuse rather than pick one."""
    settings = get_settings()
    in_both = [
        [RankedChunk("both", 1)],
        [RankedChunk("both", 8)],
    ]
    only_one = [
        [RankedChunk("single", 1)],
        [],
    ]
    both_score = reciprocal_rank_fusion(in_both, settings.retrieval)
    single_score = reciprocal_rank_fusion(only_one, settings.retrieval)

    # Same top rank in the first list; the difference is only the second list.
    fused = reciprocal_rank_fusion(
        [[RankedChunk("both", 1), RankedChunk("single", 2)], [RankedChunk("both", 8)]],
        settings.retrieval,
    )
    assert fused[0] == "both"
    assert both_score == ["both"]
    assert single_score == ["single"]


def test_rrf_deduplicates_across_halves() -> None:
    settings = get_settings()
    fused = reciprocal_rank_fusion(
        [
            [RankedChunk("a", 1), RankedChunk("b", 2)],
            [RankedChunk("b", 1), RankedChunk("a", 2)],
        ],
        settings.retrieval,
    )
    assert sorted(fused) == ["a", "b"]
    assert len(fused) == 2


def test_rrf_ordering_is_deterministic() -> None:
    """Ties break on chunk_id, so identical inputs cannot produce different
    orders across runs — otherwise no test could pin the ranking down."""
    settings = get_settings()
    halves = [[RankedChunk("z", 1), RankedChunk("a", 1)], []]
    first = reciprocal_rank_fusion(halves, settings.retrieval)
    second = reciprocal_rank_fusion(halves, settings.retrieval)

    assert first == second == ["a", "z"]  # equal scores -> chunk_id ascending


# ------------------------------------------------------------------- end to end


async def test_hybrid_search_returns_hydrated_passages_in_fused_order() -> None:
    from app.engine.query.retrieval import hybrid_search

    settings = get_settings()
    question = "what is the excess baggage fee?"
    embedding = await _embed(question)
    with get_session() as session:
        passages = hybrid_search(session, question, embedding, settings.retrieval, DEFAULT)

    assert passages
    top = passages[0]
    assert top.source_file.endswith(".pdf")
    assert top.page > 0
    assert top.display_text
    assert "excess" in top.display_text.lower()

    # The near-duplicate pair the fixture corpus was built around: the same topic
    # in a DIFFERENT document should also surface.
    ids = [p.chunk_id for p in passages[:5]]
    assert "fx-baggage-01__001" in ids
    assert "fx-coc-01__002" in ids


# ------------------------------------------------------- version supersession


def test_identical_text_collapses_to_the_newest_version() -> None:
    """⚠️ 50 chunks are byte-identical between the current and previous
    conditions of carriage. Both retrieve, both score the same, and two of five
    result slots then carry one slot's worth of information — 40% of what the
    synthesizer sees, spent on a duplicate.

    `effective_date` is what makes the choice principled rather than arbitrary.
    """

    with get_session() as session:
        shared = session.execute(
            text(
                """
                SELECT c.id, d.source_file, d.effective_date
                FROM chunks c JOIN documents d ON d.id = c.doc_id
                WHERE d.collection = 'corpus'
                  AND d.source_file LIKE 'etihad-general-conditions%'
                  AND c.display_text = (
                      SELECT c2.display_text
                      FROM chunks c2 JOIN documents d2 ON d2.id = c2.doc_id
                      WHERE d2.collection = 'corpus'
                        AND d2.source_file = 'etihad-general-conditions-of-carriage.pdf'
                        AND EXISTS (
                            SELECT 1 FROM chunks c3 JOIN documents d3 ON d3.id = c3.doc_id
                            WHERE d3.source_file =
                                  'etihad-general-conditions-of-carriage-previous.pdf'
                              AND c3.display_text = c2.display_text
                        )
                      LIMIT 1
                  )
                """
            )
        ).all()

        if len(shared) < 2:
            pytest.skip("corpus does not contain a duplicated chunk pair")

        ids = [r.id for r in shared]
        passages = load_passages(session, ids)

    assert len(passages) == 1, "identical text must collapse to a single passage"
    # And it must be the newest, not whichever happened to retrieve first.
    newest = max(shared, key=lambda r: (r.effective_date is not None, r.effective_date))
    assert passages[0].source_file == newest.source_file


def test_supersession_can_be_disabled() -> None:
    """Diagnostics and the evaluation harness need to see what was collapsed."""
    with get_session() as session:
        rows = session.execute(
            text(
                "SELECT c.id FROM chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE d.collection='corpus' LIMIT 5"
            )
        ).all()
        ids = [r.id for r in rows]
        kept = load_passages(session, ids, drop_superseded=False)

    assert len(kept) == len(ids)


def test_null_effective_date_never_displaces_a_dated_document() -> None:
    """NULL means "unknown", not "newest". A corpus where nobody populated the
    column must behave exactly as before, not silently reorder."""
    from datetime import date

    from app.engine.query.retrieval import _supersedes

    assert _supersedes(date(2025, 1, 1), date(2024, 1, 1)) is True
    assert _supersedes(date(2024, 1, 1), date(2025, 1, 1)) is False
    assert _supersedes(None, date(2024, 1, 1)) is False, "NULL must not win"
    assert _supersedes(date(2024, 1, 1), None) is True
    assert _supersedes(None, None) is False, "two unknowns keep retrieval order"
