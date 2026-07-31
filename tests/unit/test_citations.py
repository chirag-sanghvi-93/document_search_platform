"""Citation assembly — no model, no database.

⚠️ Almost every failure this file guards against produces output that *looks
right*. A spurious marker parses cleanly. An unmerged page reads as two
corroborating sources. A padded source list looks thorough. None of it raises,
and none of it is visible in a fluent answer — which is exactly why the rules are
asserted here rather than trusted to review.

See doc/components/05-citation-handling.md.
"""

from __future__ import annotations

from app.engine.query import citations
from app.shared.types import Passage


def _passage(
    chunk_id: str,
    *,
    source_file: str = "coc.pdf",
    page: int = 1,
    text: str = "Some clause text.",
    section: str = "",
) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id="d1",
        source_file=source_file,
        title="Conditions of Carriage",
        page=page,
        display_text=text,
        score=0.9,
        section=section,
    )


# ------------------------------------------------------- the collision guard


def test_bracketed_numerals_in_source_text_are_neutralised() -> None:
    """⚠️ The subtlest failure in the whole citation path.

    Airline and insurance conditions are full of their own bracketed numerals.
    If the model echoes "as set out in [2]" back from a passage, the result is a
    marker that was never a marker: it parses, points at passage 2, and means
    nothing. Every bracket the model sees must be one we introduced.
    """
    assert citations.sanitize_for_prompt("as set out in [2] above") == "as set out in (2) above"


def test_sanitising_leaves_ordinary_text_alone() -> None:
    assert citations.sanitize_for_prompt("no brackets here") == "no brackets here"


def test_the_reader_still_sees_the_original_text() -> None:
    """Sanitising is for the prompt only. The quote under a citation must be the
    document's own wording, brackets and all."""
    passage = _passage("c1", text="Cover applies as set out in [2].")

    result = citations.build("The rule applies [1].", [passage])

    assert result.citations[0].quote == "Cover applies as set out in [2]."


# ------------------------------------------------------------- renumbering


def test_markers_are_renumbered_by_first_appearance() -> None:
    """⚠️ Passages arrive ranked by relevance; the model does not write in that
    order. It may open with passage 3 and never use passage 2 — so the reader
    meets [3] before [1] with [2] missing, which reads as broken and undermines
    the one feature meant to build confidence."""
    passages = [
        _passage("c1", page=1),
        _passage("c2", page=2),
        _passage("c3", page=3),
    ]

    result = citations.build("First this [3]. Then this [1].", passages)

    assert result.answer == "First this [1]. Then this [2]."
    assert [c.page for c in result.citations] == [3, 1]


def test_renumbering_cannot_collide_with_itself() -> None:
    """A swap — 1 becomes 2 and 2 becomes 1 — done by successive replacement
    would turn both into the same number. One pass over the original string is
    what prevents it."""
    passages = [_passage("c1", page=10), _passage("c2", page=20)]

    result = citations.build("Second [2] then first [1].", passages)

    assert result.answer == "Second [1] then first [2]."
    assert [c.page for c in result.citations] == [20, 10]


def test_a_repeated_marker_keeps_one_number() -> None:
    passages = [_passage("c1", page=5)]
    result = citations.build("A claim [1]. Another claim [1].", passages)

    assert result.answer == "A claim [1]. Another claim [1]."
    assert len(result.citations) == 1


# ------------------------------------------------------------------ merging


def test_two_chunks_on_the_same_page_become_one_citation() -> None:
    """⚠️ Unmerged, the reader sees [1] and [2] pointing at the same page — which
    reads as two independent sources corroborating one another. It is one source
    cited twice, and that is the opposite of the assurance a citation gives."""
    passages = [
        _passage("c1", page=14, section="4.2 Exclusions"),
        _passage("c2", page=14, section="4.3 Limits"),
    ]

    result = citations.build("Claim one [1]. Claim two [2].", passages)

    assert len(result.citations) == 1
    assert result.answer == "Claim one [1]. Claim two [1]."


def test_the_same_page_in_different_files_stays_two_citations() -> None:
    passages = [
        _passage("c1", source_file="a.pdf", page=14),
        _passage("c2", source_file="b.pdf", page=14),
    ]

    result = citations.build("One [1]. Two [2].", passages)

    assert len(result.citations) == 2


def test_section_is_displayed_but_does_not_split_a_citation() -> None:
    """The reader's unit of verification is a page: they turn to page 14. Section
    tells them where to look once there — it is not what identifies the source."""
    passages = [
        _passage("c1", page=14, section="4.2 Exclusions"),
        _passage("c2", page=14, section="4.3 Limits"),
    ]

    result = citations.build("One [1]. Two [2].", passages)

    assert len(result.citations) == 1
    assert result.citations[0].section == "4.2 Exclusions"


# ----------------------------------------------------------- what is excluded


def test_a_retrieved_but_uncited_passage_is_not_listed() -> None:
    """⚠️ The tempting alternative is listing everything retrieved "for
    transparency". That is actively misleading: a source list asserts *this is
    where the answer came from*, so padding it implies support that does not
    exist. The full retrieved set belongs in the trace."""
    passages = [_passage("c1", page=1), _passage("c2", page=2)]

    result = citations.build("Only the first is used [1].", passages)

    assert len(result.citations) == 1
    assert result.citations[0].page == 1


def test_a_marker_pointing_at_no_passage_is_dropped_and_recorded() -> None:
    """⚠️ A fabricated [9] would otherwise reach the reader as a plausible-looking
    reference. It is recorded rather than silently removed: a non-zero rate here
    means the synthesizer is inventing references, and nothing about the answer's
    fluency would show it."""
    passages = [_passage("c1", page=1)]

    result = citations.build("Real [1]. Invented [9].", passages)

    assert result.invalid_markers == (9,)
    assert "[9]" not in result.answer
    assert len(result.citations) == 1


def test_dropping_an_invalid_marker_leaves_clean_punctuation() -> None:
    passages = [_passage("c1", page=1)]
    result = citations.build("A claim [7].", passages)

    assert result.answer == "A claim."


def test_an_answer_with_no_markers_produces_no_citations() -> None:
    passages = [_passage("c1", page=1)]
    result = citations.build("The documents do not cover this.", passages)

    assert result.citations == ()
    assert not result.has_citations


# ------------------------------------------------------------- near misses


def test_near_misses_are_numbered_zero_so_they_cannot_pass_as_sources() -> None:
    """⚠️ These are offered when nothing answered the question. The risk is a
    reader skimming the label and reading them as support, so they must be
    structurally distinguishable, not merely labelled differently."""
    passages = [_passage("c1", page=22), _passage("c2", page=19)]

    misses = citations.near_misses(passages)

    assert all(c.number == 0 for c in misses)
    assert [c.page for c in misses] == [22, 19]


def test_near_misses_are_deduplicated_by_page_and_capped() -> None:
    passages = [_passage(f"c{i}", page=1) for i in range(5)]
    assert len(citations.near_misses(passages)) == 1

    many = [_passage(f"c{i}", page=i) for i in range(10)]
    assert len(citations.near_misses(many, limit=3)) == 3


# ---------------------------------------------------------------- rendering


def test_rendering_shows_title_page_and_section() -> None:
    passages = [_passage("c1", page=14, section="4.2 Exclusions", text="Cover does not apply.")]
    result = citations.build("A claim [1].", passages)

    rendered = citations.render(result.citations)

    assert "[1] Conditions of Carriage · p.14 · 4.2 Exclusions" in rendered
    assert '"Cover does not apply."' in rendered


def test_a_long_quote_is_truncated() -> None:
    passages = [_passage("c1", text="word " * 300)]
    result = citations.build("A claim [1].", passages)

    assert len(result.citations[0].quote) < 500
    assert result.citations[0].quote.endswith("…")


# ------------------------------------------- metadata survival through the pipeline


def test_reranking_preserves_every_passage_field_except_the_score() -> None:
    """⚠️ A real defect, and an invisible one.

    Re-ranking rebuilt each Passage by enumerating its fields, so adding
    `section` to the dataclass silently dropped it: retrieval carried the
    heading through, re-ranking threw it away, and every citation rendered
    without the line telling the reader where to look on the page. Nothing
    raised, no test failed, the field was simply gone.

    Asserted against the dataclass's own field list so that the NEXT field added
    to Passage is covered by this test without anyone remembering to extend it.
    """
    import dataclasses

    from app.engine.query import rerank as rerank_module
    from app.shared.config import RetrievalSettings

    class _StubEncoder:
        """Scores everything above the floor. A real cross-encoder would load a
        model and might score this test passage below `keep_floor`, filtering it
        out before the rebuild under test ever ran."""

        def predict(self, pairs: list[object]) -> list[float]:
            return [0.9] * len(pairs)

    original = _passage("c1", page=14, section="4.2 Exclusions")
    saved = rerank_module._encoder
    rerank_module._encoder = _StubEncoder()  # type: ignore[assignment]
    try:
        result = rerank_module.rerank("a question", [original], RetrievalSettings())
    finally:
        rerank_module._encoder = saved

    assert result, "the stub scores above the floor, so the passage must survive"
    for f in dataclasses.fields(original):
        if f.name == "score":
            continue
        assert getattr(result[0], f.name) == getattr(original, f.name), (
            f"re-ranking dropped Passage.{f.name}"
        )
    assert result[0].score == 0.9, "the score IS meant to change; everything else is not"
