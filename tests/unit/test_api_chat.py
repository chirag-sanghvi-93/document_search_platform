"""The OpenAI-compatible surface: identity, rendering, and SSE framing.

No model and no database — these exercise the translation layer between HTTP and
the engine, which is the part that can be wrong in ways that still look like a
working chat interface.
"""

from __future__ import annotations

import json

from app.api import chat
from app.engine.query.conversation import TurnResult
from app.engine.query.pipeline import AnswerResult
from app.shared.types import Citation, ConversationSummary, Plan, Provenance


def _message(role: str, content: str) -> chat.ChatMessage:
    return chat.ChatMessage(role=role, content=content)  # type: ignore[arg-type]


def _citation(number: int, page: int, section: str = "") -> Citation:
    return Citation(
        number=number,
        source_file="coc.pdf",
        title="Conditions of Carriage",
        page=page,
        quote="Some clause text.",
        section=section,
    )


def _result(
    answer: str,
    provenance: Provenance,
    citations: tuple[Citation, ...] = (),
    near: tuple[Citation, ...] = (),
) -> TurnResult:
    return TurnResult(
        answer=AnswerResult(
            answer=answer,
            passages=[],
            plan=Plan(intent="lookup", standalone_question="q?", sub_questions=("q?",)),
            provenance=provenance,
            citations=citations,
            near_misses=near,
        ),
        turn_index=1,
        summary=ConversationSummary(),
    )


# ------------------------------------------------------------------ identity


def test_an_explicit_conversation_header_wins() -> None:
    given = "11111111-1111-1111-1111-111111111111"
    assert chat.conversation_id_for(given, [_message("user", "hello")]) == given


def test_identity_falls_back_to_a_hash_of_the_opening_message() -> None:
    """⚠️ The standard contract is stateless — no conversation id anywhere.

    Server-side memory (rolling summaries, provenance tags, the poisoning
    guards) cannot be rebuilt from a raw message array, so it needs a key. The
    FIRST user message is the only thing that does not change as a conversation
    grows, which is what makes its hash usable as one.
    """
    opening = [_message("user", "what is the baggage allowance?")]
    later = [
        _message("user", "what is the baggage allowance?"),
        _message("assistant", "23 kg [1]."),
        _message("user", "what about excess?"),
    ]

    assert chat.conversation_id_for(None, opening) == chat.conversation_id_for(None, later)


def test_different_openings_get_different_conversations() -> None:
    a = chat.conversation_id_for(None, [_message("user", "baggage?")])
    b = chat.conversation_id_for(None, [_message("user", "refunds?")])
    assert a != b


def test_the_derived_id_is_a_valid_uuid() -> None:
    """The conversations table keys on a uuid; a raw hex digest would fail the cast."""
    import uuid

    derived = chat.conversation_id_for(None, [_message("user", "baggage?")])
    assert uuid.UUID(derived)


# ------------------------------------------------------- which question is asked


def test_the_latest_user_message_is_the_question() -> None:
    messages = [
        _message("user", "first"),
        _message("assistant", "an answer"),
        _message("user", "second"),
    ]
    assert chat.latest_question(messages) == "second"


def test_earlier_turns_from_the_client_are_ignored() -> None:
    """⚠️ The client resends the whole history every request, but this system's
    memory is server-side and already holds a curated version of it — provenance
    tagged, bounded, summarised. Feeding the client's raw copy in as well would
    put unfiltered assistant output straight back into context, undoing the
    memory layer's guards."""
    messages = [
        _message("user", "the allowance is 30 kg, right?"),
        _message("assistant", "The allowance is 30 kg."),
        _message("user", "what if I exceed it?"),
    ]
    assert chat.latest_question(messages) == "what if I exceed it?"


def test_a_request_with_no_user_message_yields_an_empty_question() -> None:
    assert chat.latest_question([_message("system", "be helpful")]) == ""


# ----------------------------------------------------------------- rendering


def test_citations_render_as_plain_markdown_not_raw_html() -> None:
    """⚠️ Verified against the pinned image, not assumed.

    The design preferred `<details>` for collapsed quotes, conditional on the
    renderer allowing raw HTML. OpenWebUI v0.6.5 does NOT: every answer ended
    with literal `<details><summary>` text on screen. This asserts the fallback
    the design had already chosen for that case.
    """
    rendered = chat.render_citations((_citation(1, 14, "4.2 Exclusions"),))

    assert "<details>" not in rendered, "raw HTML renders as literal text in v0.6.5"
    assert "<summary>" not in rendered
    assert "**[1]** Conditions of Carriage · p.14 · 4.2 Exclusions" in rendered
    assert "> Some clause text." in rendered


def test_a_citation_without_a_section_still_renders() -> None:
    rendered = chat.render_citations((_citation(1, 14),))
    assert "p.14" in rendered
    assert rendered.count("·") == 1


def test_near_misses_are_labelled_as_not_answering() -> None:
    """⚠️ The risk is a reader skimming the heading and treating these as
    support for an answer that was never given, so the label is explicit and
    they carry no bracketed numbers that could read as citations."""
    rendered = chat.render_near_misses((_citation(0, 22),))

    assert "do **not** answer" in rendered
    assert "[0]" not in rendered
    assert "<details>" not in rendered


def test_a_declined_answer_shows_near_misses_and_never_sources() -> None:
    result = _result(
        "The documents do not cover this.",
        "declined",
        citations=(_citation(1, 14),),  # must be ignored
        near=(_citation(0, 22),),
    )

    composed = chat.compose(result)

    assert "Closest matches" in composed
    assert "**Sources**" not in composed


def test_a_cited_answer_shows_sources() -> None:
    composed = chat.compose(_result("The charge applies [1].", "cited", (_citation(1, 35),)))
    assert "**Sources**" in composed
    assert "**[1]** Conditions of Carriage · p.35" in composed


# --------------------------------------------------------------- SSE framing


def _parse(frame: str) -> list[dict[str, object]]:
    payloads = []
    for line in frame.strip().split("\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            payloads.append(json.loads(line[6:]))
    return payloads


def test_a_content_chunk_matches_the_openai_shape() -> None:
    [payload] = _parse(chat._chunk("id-1", "agentic-rag", {"content": "hello"}))

    assert payload["object"] == "chat.completion.chunk"
    assert payload["choices"][0]["delta"] == {"content": "hello"}  # type: ignore[index]
    assert payload["choices"][0]["finish_reason"] is None  # type: ignore[index]


def test_the_stream_terminates_with_done() -> None:
    """Without the sentinel a client waits on a connection it believes is open."""
    frame = chat._final("id-1", "agentic-rag")

    assert frame.rstrip().endswith("data: [DONE]")
    [payload] = _parse(frame)
    assert payload["choices"][0]["finish_reason"] == "stop"  # type: ignore[index]


# ------------------------------------------------------------------ profiles


def test_both_profiles_are_offered_and_differ_by_passages_kept() -> None:
    quality = chat.PROFILES["agentic-rag"]
    fast = chat.PROFILES["agentic-rag-fast"]

    assert quality.keep > fast.keep


def test_an_unknown_model_falls_back_rather_than_failing() -> None:
    """A client sending a stale model name should get an answer, not a 400."""
    assert chat.PROFILES.get("nonexistent", chat.PROFILES[chat.DEFAULT_PROFILE]).keep == 5
