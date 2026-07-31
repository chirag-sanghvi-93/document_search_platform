"""Conversation memory: window selection, rendering, and the poisoning guards.

⚠️ Most of this file exists for one reason: **single-turn testing cannot find
the bug this component is about.** Every turn judged on its own looks perfectly
grounded — the failure is that turn 3's hedge becomes turn 5's premise. So the
assertions here are deliberately about what survives *across* turns, not about
whether any one turn is well formed.

See doc/components/04-conversation-memory.md §6.
"""

from __future__ import annotations

from app.engine.query import memory
from app.shared.config import ConversationSettings, OllamaSettings
from app.shared.types import ConversationSummary, Provenance, Role, Turn

OLLAMA = OllamaSettings()


def _turn(index: int, role: Role, content: str, provenance: Provenance | None = None) -> Turn:
    return Turn(
        id=f"t{index}",
        conversation_id="c1",
        turn_index=index,
        role=role,
        content=content,
        provenance=provenance,
    )


# ------------------------------------------------- the verbatim window


def test_the_window_is_bounded_by_tokens_not_turn_count() -> None:
    """⚠️ Turn count is the proxy this design explicitly rejects.

    One turn is 50 tokens; the next is 2,000 because a clause was pasted in. Four
    turns can therefore mean 200 tokens or 8,000, and only one of those fits. The
    resource actually running out is context, so it is measured directly.
    """
    settings = ConversationSettings(verbatim_budget_tokens=40, verbatim_turns=4)
    turns = [_turn(i, "user", "word " * 60) for i in range(4)]

    kept, evicted, used = memory.select_verbatim(turns, settings=settings, ollama=OLLAMA)

    assert len(kept) == 1, "the budget, not the turn cap, must be what stopped it"
    assert len(evicted) == 3
    assert used > 0


def test_the_window_keeps_the_most_recent_turns() -> None:
    """Reference resolution depends on recency — "what about the other one?"
    refers to the last thing said, not the first."""
    settings = ConversationSettings(verbatim_budget_tokens=10_000, verbatim_turns=2)
    turns = [_turn(i, "user", f"question {i}") for i in range(5)]

    kept, evicted, _ = memory.select_verbatim(turns, settings=settings, ollama=OLLAMA)

    assert [t.turn_index for t in kept] == [3, 4], "kept turns must be in chronological order"
    assert [t.turn_index for t in evicted] == [0, 1, 2]


def test_one_oversized_turn_is_kept_rather_than_leaving_an_empty_window() -> None:
    """Dropping it would leave the pronoun in the next question unresolvable,
    which is worse than being over budget."""
    settings = ConversationSettings(verbatim_budget_tokens=5, verbatim_turns=4)
    turns = [_turn(0, "user", "word " * 500)]

    kept, evicted, _ = memory.select_verbatim(turns, settings=settings, ollama=OLLAMA)

    assert len(kept) == 1
    assert evicted == ()


def test_an_empty_history_produces_an_empty_window() -> None:
    kept, evicted, used = memory.select_verbatim([], settings=ConversationSettings(), ollama=OLLAMA)
    assert (kept, evicted, used) == ((), (), 0)


# ------------------------------------------------------------- rendering


def test_a_declined_answer_is_rendered_as_declined() -> None:
    """⚠️ Poisoning vector 1, blocked at the point of rendering.

    Turn 3 refuses. By turn 5 that refusal is just text sitting in the context,
    indistinguishable from a cited answer — so the model reasons from it as
    though it were established. The tag is what keeps a refusal legible."""
    turns = (
        _turn(0, "user", "what is the surfboard fee?"),
        _turn(1, "assistant", "I could not find a surfboard fee.", provenance="declined"),
    )

    rendered = memory.render_turns(turns)

    assert "[not answered from the documents]" in rendered


def test_a_hedged_answer_is_rendered_as_hedged() -> None:
    turns = (_turn(1, "assistant", "Partly covered.", provenance="hedged"),)
    assert "some claims were retracted" in memory.render_turns(turns)


def test_a_cited_answer_carries_no_qualifier() -> None:
    turns = (_turn(1, "assistant", "The charge is per tariff [1].", provenance="cited"),)
    assert memory.render_turns(turns) == "assistant: The charge is per tariff [1]."


def test_user_turns_are_never_tagged() -> None:
    """Provenance describes how an ANSWER was grounded. A question has none, and
    tagging one would imply the user's own words had been verified."""
    turns = (_turn(0, "user", "what is the fee?"),)
    assert memory.render_turns(turns) == "user: what is the fee?"


def test_empty_summary_sections_render_explicitly() -> None:
    """An absent heading is ambiguous — nothing declined, or the summariser
    dropped the field? That ambiguity is how `declined` quietly disappears."""
    summary = ConversationSummary(parameters=("route ABZ-LHR",))

    rendered = memory.render_summary(summary)

    assert "Asked but NOT answered: (none)" in rendered
    assert "route ABZ-LHR" in rendered


# -------------------------------------------------------------- merging


def test_a_declined_item_is_never_lost_when_the_summariser_forgets_it() -> None:
    """⚠️ Poisoning vector 2 — the nastiest one.

    Compression strips qualifiers first: they are the least information-dense
    part of a sentence. So "I couldn't find a surfboard fee" tends to become
    "surfboard fee: not covered", or to vanish. Merging in code means a
    forgetful pass cannot erase the record that something went unanswered.
    """
    previous = ConversationSummary(declined=("surfboard fee — not found",))
    forgetful = ConversationSummary(topics=("baggage allowance",))

    merged = memory.merge_summaries(previous, forgetful)

    assert "surfboard fee — not found" in merged.declined


def test_an_established_parameter_survives_a_forgetful_pass() -> None:
    """The parameter is what the next follow-up resolves against. Losing it makes
    the follow-up silently answer about something else."""
    previous = ConversationSummary(parameters=("route ABZ-LHR", "Economy fare"))
    forgetful = ConversationSummary(parameters=("Economy fare",))

    merged = memory.merge_summaries(previous, forgetful)

    assert merged.parameters == ("route ABZ-LHR", "Economy fare")


def test_a_question_later_answered_moves_out_of_declined() -> None:
    """The one legitimate transition: the user asked again and it was found.
    The reverse never happens implicitly."""
    previous = ConversationSummary(declined=("baggage allowance",))
    updated = ConversationSummary(topics=("baggage allowance",))

    merged = memory.merge_summaries(previous, updated)

    assert merged.declined == ()
    assert merged.topics == ("baggage allowance",)


def test_merging_does_not_duplicate() -> None:
    previous = ConversationSummary(topics=("baggage",))
    updated = ConversationSummary(topics=("baggage", "refunds"))

    assert memory.merge_summaries(previous, updated).topics == ("baggage", "refunds")


# -------------------------------------------------------------- trimming

TOKENIZER = OLLAMA.tokenizer_repo


def _tokens(summary: ConversationSummary) -> int:
    from app.shared.tokens import count_tokens

    return count_tokens(memory.render_summary(summary), TOKENIZER)


def test_a_summary_within_budget_is_returned_untouched() -> None:
    summary = ConversationSummary(parameters=("Economy fare",), topics=("baggage",))
    assert memory.trim_summary(summary, max_tokens=400, tokenizer_repo=TOKENIZER) == summary


def test_an_oversized_summary_is_trimmed_to_the_budget() -> None:
    """⚠️ Without this the summary is unbounded.

    `merge_summaries` unions on every eviction and never removes anything. The
    failure is not a crash — the summary is prepended to the planner and
    synthesizer prompts, so it silently eats the window that evidence needs and
    the system starts answering from conversation rather than from documents.
    """
    summary = ConversationSummary(topics=tuple(f"topic number {i}" for i in range(200)))

    trimmed = memory.trim_summary(summary, max_tokens=100, tokenizer_repo=TOKENIZER)

    assert _tokens(trimmed) <= 100
    assert len(trimmed.topics) < 200


def test_topics_are_sacrificed_before_declined_and_parameters() -> None:
    """⚠️ The priority order is the design made executable.

    `parameters` is what a follow-up resolves against, and `declined` is the
    anti-poisoning field — dropping a "could not find" is precisely how a hedge
    becomes a finding. `topics` is the most recoverable: it says only what was
    discussed, and recent turns are still in the verbatim window.
    """
    summary = ConversationSummary(
        parameters=("route ABZ-LHR", "Economy fare"),
        topics=tuple(f"topic number {i}" for i in range(200)),
        declined=("surfboard fee — not found",),
        open_threads=("excess charges",),
    )

    trimmed = memory.trim_summary(summary, max_tokens=90, tokenizer_repo=TOKENIZER)

    assert trimmed.parameters == ("route ABZ-LHR", "Economy fare")
    assert trimmed.declined == ("surfboard fee — not found",)
    assert len(trimmed.topics) < 200


def test_the_oldest_entries_go_first_within_a_field() -> None:
    """Recent turns are still available verbatim, so what the summary uniquely
    carries is the older context — but between two summary entries, the newer is
    the one more likely to still matter."""
    summary = ConversationSummary(topics=tuple(f"topic number {i}" for i in range(60)))

    trimmed = memory.trim_summary(summary, max_tokens=60, tokenizer_repo=TOKENIZER)

    assert "topic number 59" in trimmed.topics
    assert "topic number 0" not in trimmed.topics


def test_a_long_conversation_stays_bounded_across_many_evictions() -> None:
    """⚠️ The case a single eviction event cannot demonstrate.

    Each pass merges and trims. The point is that the ceiling holds after fifty
    of them rather than drifting upward — an unbounded union looks perfectly
    healthy for the first few turns, which is exactly why one eviction proved
    nothing.
    """
    summary = ConversationSummary()

    for turn in range(50):
        incoming = ConversationSummary(
            topics=(f"topic from turn {turn}",),
            open_threads=(f"thread from turn {turn}",),
        )
        summary = memory.trim_summary(
            memory.merge_summaries(summary, incoming),
            max_tokens=120,
            tokenizer_repo=TOKENIZER,
        )

    assert _tokens(summary) <= 120, "the summary grew past its ceiling over a long conversation"
    assert summary.topics, "trimming must not empty the summary entirely"
    assert summary.open_threads, "no single field may be annihilated while another grows"


def test_a_parameter_established_early_survives_a_long_conversation() -> None:
    """The whole reason `parameters` is trimmed last: it is what turn 40 resolves
    "the same route" against, and it was established at turn 1."""
    summary = ConversationSummary(parameters=("route ABZ-LHR",))

    for turn in range(50):
        summary = memory.trim_summary(
            memory.merge_summaries(summary, ConversationSummary(topics=(f"topic {turn}",))),
            max_tokens=120,
            tokenizer_repo=TOKENIZER,
        )

    assert "route ABZ-LHR" in summary.parameters


# ------------------------------------------------------------ persistence


def test_a_summary_survives_a_round_trip() -> None:
    summary = ConversationSummary(
        parameters=("route ABZ-LHR",),
        topics=("baggage",),
        declined=("surfboard fee",),
        open_threads=("excess charges",),
    )
    import json

    assert memory.summary_from_mapping(json.loads(memory.summary_to_json(summary))) == summary


def test_the_summarisers_own_key_names_are_accepted() -> None:
    """The prompt asks for descriptive keys and storage uses short ones. A prompt
    reworded in the registry — editable without a deploy — must not silently
    produce a summary that parses to empty."""
    raw = {
        "parameters_established": ["Economy fare"],
        "topics_covered": ["baggage"],
        "declined_unanswered": ["surfboard fee"],
        "open_threads": [],
    }

    parsed = memory.summary_from_mapping(raw)

    assert parsed.parameters == ("Economy fare",)
    assert parsed.declined == ("surfboard fee",)


def test_a_corrupt_summary_degrades_to_empty_rather_than_raising() -> None:
    """Losing memory is a worse answer. Raising is a broken one."""
    assert memory.summary_from_mapping("not a mapping").is_empty
    assert memory.summary_from_mapping(None).is_empty


def test_a_single_string_where_a_list_was_asked_for_is_accepted() -> None:
    """Models return one string instead of a one-item list constantly."""
    parsed = memory.summary_from_mapping({"topics": "baggage"})
    assert parsed.topics == ("baggage",)
