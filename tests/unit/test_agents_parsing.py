"""Agent output parsing and degradation — no model, no database.

These are the tests that can be strict, because they exercise the code that
turns an untrusted model response into a typed structure. Everything about the
read path's reliability rests on this layer refusing to pass through a
half-understood plan.
"""

from __future__ import annotations

import pytest

from app.engine.query.agents import (
    AgentError,
    CallLog,
    _corpus_identifiers,
    _extract_json,
    _format_passages,
    _parse_plan,
    _reject_invented_scope,
)
from app.shared.types import Passage


def _passage(chunk_id: str, display: str, embedding_text: str) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id="d1",
        source_file="f.pdf",
        title="T",
        page=1,
        display_text=display,
        score=0.9,
    )


# ------------------------------------------------------------------ JSON extraction


def test_json_is_extracted_from_surrounding_prose() -> None:
    """Reasoning models wrap their answer in commentary even when told not to,
    so the first {...} block is taken rather than assuming the whole response
    parses."""
    raw = 'Sure! Here is the plan:\n{"intent": "lookup"}\nHope that helps.'
    assert _extract_json(raw) == {"intent": "lookup"}


def test_missing_json_raises_a_named_error() -> None:
    with pytest.raises(AgentError, match="no JSON object"):
        _extract_json("I could not determine a plan.")


def test_a_literal_newline_inside_a_string_value_is_tolerated() -> None:
    """⚠️ Observed against the real corpus: the verifier returned a two-paragraph
    answer with a raw newline in it, strict parsing rejected the object, and a
    good answer was handed back marked "could not be verified"."""
    raw = '{"verified_answer": "First line.\nSecond line.", "claims_retracted": 0}'
    assert _extract_json(raw)["verified_answer"] == "First line.\nSecond line."


def test_malformed_json_raises_a_named_error() -> None:
    with pytest.raises(AgentError, match="malformed JSON"):
        _extract_json('{"intent": "lookup",}')


def test_a_json_array_is_rejected_as_not_an_object() -> None:
    """The extractor searches for `{...}` specifically, so an array never even
    reaches the type check — it fails at extraction. Either way it is refused;
    this asserts which error actually fires rather than which one looks tidier."""
    with pytest.raises(AgentError, match="no JSON object"):
        _extract_json("[1, 2, 3]")


# ----------------------------------------------------------------- plan parsing


def test_plan_parses_intent_and_sub_questions() -> None:
    raw = """
    {"intent": "comparison",
     "standalone_question": "How do A and B differ?",
     "sub_questions": ["what is A?", "what is B?"]}
    """
    plan = _parse_plan(raw, "how do they differ?", cap=4)
    assert plan.intent == "comparison"
    assert plan.standalone_question == "How do A and B differ?"
    assert plan.sub_questions == ("what is A?", "what is B?")


def test_plan_rejects_an_invalid_intent() -> None:
    """A misread intent changes the whole control flow — out_of_scope skips
    retrieval entirely — so an unrecognised value must fail loudly rather than
    default to something plausible."""
    with pytest.raises(AgentError, match="invalid intent"):
        _parse_plan('{"intent": "maybe", "sub_questions": ["x"]}', "q", cap=4)


def test_plan_enforces_the_sub_question_cap_by_truncating() -> None:
    raw = '{"intent": "comparison", "sub_questions": ["a","b","c","d","e","f"]}'
    plan = _parse_plan(raw, "q", cap=4)
    assert len(plan.sub_questions) == 4


def test_out_of_scope_needs_no_sub_questions() -> None:
    plan = _parse_plan('{"intent": "out_of_scope", "sub_questions": []}', "weather?", cap=4)
    assert plan.intent == "out_of_scope"
    assert plan.sub_questions == ()


def test_answerable_plan_with_no_sub_questions_falls_back_to_the_question() -> None:
    """The model classified it answerable but gave nothing to search for.
    Searching the original question is strictly better than failing."""
    plan = _parse_plan('{"intent": "lookup", "sub_questions": []}', "what is the fee?", cap=4)
    assert plan.sub_questions == ("what is the fee?",)


def test_plan_falls_back_to_the_original_question_when_rewriting_is_missing() -> None:
    plan = _parse_plan('{"intent": "lookup", "sub_questions": ["x"]}', "original?", cap=4)
    assert plan.standalone_question == "original?"


def test_sub_questions_must_be_a_list() -> None:
    with pytest.raises(AgentError, match="must be a list"):
        _parse_plan('{"intent": "lookup", "sub_questions": "not a list"}', "q", cap=4)


# -------------------------------------------- invented scope (the search query)

#: Shaped like the real thing: one line per document, each opening with the
#: organisation that owns it. That shape is what the planner anchors on.
CORPUS = (
    "This corpus covers:\n"
    "  · Delta Air Lines' domestic conditions of carriage. It applies to passengers\n"
    "    travelling within the United States and covers baggage, fares and refunds.\n"
    "  · Etihad Airways travel insurance policy handbook. It details coverage terms,\n"
    "    exclusions and claims procedures.\n"
)


def test_corpus_identifiers_finds_organisation_names() -> None:
    identifiers = _corpus_identifiers(CORPUS)
    assert "delta air lines" in identifiers
    assert "delta" in identifiers
    assert "etihad" in identifiers


def test_corpus_identifiers_ignores_ordinary_sentence_openers() -> None:
    """ "This corpus covers" and "It applies" are capitalised but identify nothing.
    Treating them as corpus terms would reject almost every sub-question."""
    identifiers = _corpus_identifiers(CORPUS)
    assert "this" not in identifiers
    assert "document" not in identifiers
    assert "conditions" not in identifiers


def test_a_document_the_user_never_named_is_removed_from_the_search() -> None:
    """⚠️ The defect this guard exists for.

    A sub-question IS the search query. The planner turned a general question
    into a Delta-specific one, so retrieval returned five Delta passages, the
    Etihad clause that answered it was excluded, and the system declined on a
    corpus that contained the answer.
    """
    question = "what is the excess baggage charge?"
    subs = ("What are the rules on excess baggage in Delta Air Lines' conditions of carriage?",)

    assert _reject_invented_scope(subs, question, "", CORPUS) == (
        "What are the rules on excess baggage?",
    )


def test_only_the_narrowing_clause_is_removed_not_the_whole_question() -> None:
    """⚠️ The regression this cost, found only once conversation memory existed.

    The planner does two things in one string here: it correctly resolves "that
    allowance" into "the checked baggage allowance", AND it bolts on a document
    nobody named. Discarding the whole sub-question threw away the resolution
    too, so the search ran on the literal words "that allowance" and the
    follow-up declined.
    """
    question = "what happens if I go over that allowance?"
    subs = (
        "What happens if I exceed the checked baggage allowance "
        "on Delta Air Lines' domestic conditions of carriage?",
    )

    assert _reject_invented_scope(subs, question, "", CORPUS) == (
        "What happens if I exceed the checked baggage allowance?",
    )


def test_the_subject_survives_when_the_question_has_several_prepositions() -> None:
    """ "...on excess baggage in Delta Air Lines'..." contains two connectives.
    Cutting at the first leaves "What are the rules?" — grammatical, and about
    nothing at all."""
    subs = ("What are the fees for excess baggage according to Delta Air Lines' conditions?",)

    result = _reject_invented_scope(subs, "what is the excess baggage charge?", "", CORPUS)

    assert result == ("What are the fees for excess baggage?",)


def test_an_unstrippable_narrowing_falls_back_to_the_users_question() -> None:
    """ "What is Delta's excess baggage charge?" has the name embedded in the
    subject itself, so there is no clause to cut. The user's own words are the
    safe answer — which is what a non-agentic pipeline would have searched."""
    question = "what is the charge?"
    subs = ("What is Delta's excess baggage charge?",)

    assert _reject_invented_scope(subs, question, "", CORPUS) == (question,)


def test_a_name_the_user_did_supply_is_kept() -> None:
    """The guard removes names the model invented, not names the user chose.
    Rejecting these would break every question about a specific document."""
    question = "what does Etihad cover for lost baggage?"
    subs = ("What does the Etihad travel insurance policy cover for lost baggage?",)

    assert _reject_invented_scope(subs, question, "", CORPUS) == subs


def test_a_name_carried_over_from_the_conversation_is_kept() -> None:
    """Resolving "what about theirs?" into an explicit name is the planner doing
    its job. The name came from the conversation, not from the corpus listing."""
    subs = ("What is Delta's excess baggage charge?",)
    context = "The user has been asking about Delta baggage rules."

    assert _reject_invented_scope(subs, "what about the charge?", context, CORPUS) == subs


def test_unnarrowed_sub_questions_pass_through_untouched() -> None:
    subs = ("what is the excess baggage charge?", "what is the free baggage allowance?")
    assert _reject_invented_scope(subs, "baggage rules?", "", CORPUS) == subs


def test_two_narrowed_sub_questions_collapse_to_a_single_search() -> None:
    """Stripping each document name leaves the same question twice, and searching
    it twice would spend the shared budget on an identical query."""
    question = "what is the baggage charge?"
    subs = (
        "What is the baggage charge under Delta Air Lines' conditions of carriage?",
        "What is the baggage charge under the Etihad Airways policy?",
    )
    assert _reject_invented_scope(subs, question, "", CORPUS) == ("What is the baggage charge?",)


def test_an_empty_corpus_description_disables_the_guard() -> None:
    """With nothing to compare against there are no invented names, and every
    sub-question must survive."""
    subs = ("What is the Delta baggage charge?",)
    assert _reject_invented_scope(subs, "baggage?", "", "") == subs


# ------------------------------------------------- what the agents are shown


def test_agents_see_display_text_never_embedding_text() -> None:
    """⚠️ The structural guarantee behind honest citations.

    `embedding_text` carries a model-written contextual preamble. If the
    synthesizer saw it, that machine-generated wording could end up quoted under
    a page citation — attributing words to a page that does not contain them.
    """
    passages = [
        _passage("c1", "The actual clause text.", "PREAMBLE ADDED BY A MODEL: the clause."),
    ]
    rendered = _format_passages(passages, numbered=True)

    assert "The actual clause text." in rendered
    assert "PREAMBLE ADDED BY A MODEL" not in rendered


def test_passages_are_numbered_for_the_synthesizer() -> None:
    passages = [_passage("a", "first", "x"), _passage("b", "second", "y")]
    rendered = _format_passages(passages, numbered=True)
    assert "[1] first" in rendered
    assert "[2] second" in rendered


def test_empty_passages_render_explicitly_rather_than_blank() -> None:
    """A blank evidence block invites the model to answer from its own
    knowledge; an explicit statement does not."""
    assert _format_passages([], numbered=True) == "(no passages retrieved)"


# -------------------------------------------------------------------- call log


def test_call_log_counts_per_agent_and_in_total() -> None:
    log = CallLog()
    log.record("planner")
    log.record("retrieval-specialist")
    log.record("retrieval-specialist")

    assert log.calls == 3
    assert log.by_agent == {"planner": 1, "retrieval-specialist": 2}


# ------------------------------------- corpus-agnosticism of the scope guard

#: A corpus with NOTHING in common with the airline one. Same code must work.
MEDICAL_CORPUS = (
    "This corpus covers:\n"
    "  · Mercy General Hospital's admissions policy. It applies to patients\n"
    "    presenting at emergency and covers triage, consent and discharge.\n"
    "  · St Anne's Clinic diabetes care pathway. It applies to patients with\n"
    "    type 2 diagnosis and covers screening, medication and review.\n"
    "  · Northfield Trust patients' rights charter. It covers consent,\n"
    "    complaints and access to records for patients.\n"
)


def test_identifiers_are_derived_structurally_not_from_a_word_list() -> None:
    """⚠️ The guard must work on a corpus nobody anticipated.

    An earlier version excluded domain words by name — "passengers",
    "conditions", "coverage". That worked on airline documents and would have
    failed silently anywhere else: here, "Patients" appears in every document
    and would have been mistaken for a document identifier, stripping the
    subject out of any sub-question that mentioned patients.

    The rule is now structural: a proper noun in MOST documents is shared
    vocabulary; one in FEW is a name that tells documents apart. The words are
    corpus-specific, the test is not.
    """
    identifiers = _corpus_identifiers(MEDICAL_CORPUS)

    # Names that distinguish one document from another.
    assert "mercy" in identifiers or "mercy general hospital" in identifiers
    assert "northfield" in identifiers or "northfield trust" in identifiers

    # Ubiquitous domain vocabulary — must NOT be treated as an identifier.
    assert "patients" not in identifiers, "a word in every document identifies nothing"


def test_a_medical_question_keeps_its_subject() -> None:
    """The consequence of the above: a legitimate question survives untouched on
    a corpus the code has never seen."""
    question = "what consent is needed for patients?"
    subs = ("What consent is required for patients before treatment?",)

    assert _reject_invented_scope(subs, question, "", MEDICAL_CORPUS) == subs


def test_a_medical_document_the_user_never_named_is_still_stripped() -> None:
    """And the guard still does its job — on names it derived, not names it knew."""
    question = "what is the discharge process?"
    subs = ("What is the discharge process under Mercy General Hospital's admissions policy?",)

    result = _reject_invented_scope(subs, question, "", MEDICAL_CORPUS)

    assert result != subs, "an unnamed document must not narrow the search"
    assert "mercy" not in result[0].lower()
