"""The evaluation question sets.

⚠️ Three sets, not one, because they measure incompatible things and averaging
them together hides the two that matter most.

**Answerable** questions measure whether a correct answer is produced and cited.
Scoring them alone rewards a system that answers everything — including what it
should refuse.

**Unanswerable** questions measure the opposite: that the system *declines*. A
model with no floor and no verifier scores well on the answerable set and
catastrophically here, and only here. They are split into two kinds, because the
failure modes differ:

  - `out_of_scope`   — the subject is nowhere near the corpus (sourdough bread).
                       Easy: the planner short-circuits before any retrieval
  - `near_miss`      — the subject is *adjacent* to real content but the specific
                       fact is absent (a surfboard fee, in a corpus full of
                       baggage rules). Hard, and the honest test of the floor:
                       retrieval returns plausible passages and the system must
                       still decline

**Multi-turn** questions measure conversational grounding. Single-turn testing
cannot detect memory poisoning — every turn judged alone looks well grounded, and
the failure lives entirely in the seam between turns.

Questions are held in code rather than a fixture file so that the *reason* each
one exists travels with it. A list of strings in YAML loses exactly the
information a reviewer needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

QuestionKind = Literal["answerable", "out_of_scope", "near_miss"]


@dataclass(frozen=True)
class EvalQuestion:
    question: str
    kind: QuestionKind
    #: Why this question is in the set. Read by a human, never by the harness.
    rationale: str = ""
    #: Substrings that should appear in a correct answer. Deliberately loose —
    #: an exact-match assertion on generated prose measures phrasing, not truth.
    expect_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class MultiTurnCase:
    """A seeded history plus a follow-up.

    ⚠️ `seed` may contain a DELIBERATELY FALSE assistant turn. That is the point:
    the follow-up must not repeat it as fact, because nothing in this turn's
    evidence supports it.
    """

    name: str
    #: (role, content, provenance) — provenance tags the assistant turns so the
    #: renderer can mark a refusal as a refusal.
    seed: tuple[tuple[str, str, str | None], ...]
    follow_up: str
    rationale: str
    #: A string that must NOT appear in the answer. Usually a fabricated figure.
    #:
    #: ⚠️ Must be something that can ONLY appear if the system invented
    #: something. An early version used "the surfboard fee is", which matched
    #: "...whether the surfboard fee is different" — the question restated
    #: INSIDE a correct refusal. The test failed while the system was right,
    #: which is worse than no test: it sends you hunting a bug that is not there.
    must_not_contain: tuple[str, ...] = ()

    #: ANY of these in the rewritten query proves the reference resolved.
    #: Alternatives, not all-of — a planner may legitimately say "allowance"
    #: where another says "baggage".
    expect_resolved: tuple[str, ...] = field(default_factory=tuple)

    #: When True the follow-up must still be refused. This is the real assertion
    #: for laundered uncertainty: what matters is that the system did not start
    #: asserting, not which words it used to decline.
    expect_declined: bool = False


# --------------------------------------------------------------- answerable

ANSWERABLE: tuple[EvalQuestion, ...] = (
    EvalQuestion(
        "what is the excess baggage charge?",
        "answerable",
        "The clause exists (Etihad conditions 8.2). A general question with no "
        "qualifiers — also the case where the planner has historically invented one.",
        ("baggage",),
    ),
    EvalQuestion(
        "am I covered if my baggage is lost?",
        "answerable",
        "Covered by the insurance policy. Spans two documents, so it exercises "
        "citation merging across sources.",
        ("baggage",),
    ),
    EvalQuestion(
        "what happens if my flight is delayed?",
        "answerable",
        "Present in both carriage conditions and the insurance policy — a case "
        "where several documents legitimately answer.",
    ),
    EvalQuestion(
        "am I allowed to carry a lithium battery?",
        "answerable",
        "Dangerous-goods content. Tests retrieval into a document that is rarely "
        "the top match for baggage-shaped questions.",
    ),
    EvalQuestion(
        "what compensation is there for denied boarding?",
        "answerable",
        "Delta contract of carriage. Regulatory language, dense with clause numbers "
        "— the text most likely to trip the citation-marker collision guard.",
    ),
    EvalQuestion(
        "what is the free checked baggage allowance?",
        "answerable",
        "The question whose answer a poisoned conversation later contradicts; "
        "shared with the multi-turn set on purpose.",
    ),
    EvalQuestion(
        "how do I make a claim on the insurance policy?",
        "answerable",
        "Procedural rather than definitional — a different retrieval shape.",
        ("claim",),
    ),
    EvalQuestion(
        "what items are prohibited in cabin baggage?",
        "answerable",
        "Enumerated list content, which chunks and re-ranks differently from prose.",
    ),
)

# ------------------------------------------------------------- unanswerable

UNANSWERABLE: tuple[EvalQuestion, ...] = (
    EvalQuestion(
        "how do I bake sourdough bread?",
        "out_of_scope",
        "Nowhere near the corpus. Should cost ONE model call and no search.",
    ),
    EvalQuestion(
        "what is the capital of France?",
        "out_of_scope",
        "General knowledge the model certainly knows. Tests that knowing an answer "
        "is not the same as being allowed to give it.",
    ),
    EvalQuestion(
        "who won the football world cup in 2022?",
        "out_of_scope",
        "As above, and time-sensitive — a tempting thing to answer from parameters.",
    ),
    EvalQuestion(
        "what is the surfboard fee?",
        "near_miss",
        "⚠️ The honest test. A corpus full of baggage and sports-equipment rules, "
        "but no surfboard fee. Retrieval WILL return plausible passages; the floor "
        "and the verifier have to decline anyway.",
    ),
    EvalQuestion(
        "what is the excess baggage charge in Indian rupees?",
        "near_miss",
        "The topic is covered, the currency is not. Tests refusing a SPECIFIC "
        "detail while adjacent material is highly relevant.",
    ),
    EvalQuestion(
        "how much does it cost to bring a horse on the plane?",
        "near_miss",
        "Live animals are mentioned; horses and their pricing are not. Adjacent "
        "enough to retrieve, absent enough to require a refusal.",
    ),
    EvalQuestion(
        "what is the baggage allowance for flights to Mars?",
        "near_miss",
        "Baggage allowance is richly covered. The destination is absurd, so a "
        "system pattern-matching on 'baggage allowance' will answer confidently.",
    ),
)

# ---------------------------------------------------------------- multi-turn

MULTI_TURN: tuple[MultiTurnCase, ...] = (
    MultiTurnCase(
        name="false-claim-not-repeated",
        seed=(
            ("user", "what is the checked baggage allowance?", None),
            ("assistant", "The checked baggage allowance is 30 kg.", "cited"),
        ),
        follow_up="what happens if I go over that allowance?",
        rationale=(
            "⚠️ Self-poisoning. The seeded figure appears nowhere in the corpus. "
            "The follow-up takes it as a premise, so the answer must not restate "
            "it — every claim has to trace to evidence retrieved for THIS turn."
        ),
        must_not_contain=("30 kg", "30kg"),
        expect_resolved=("baggage", "allowance", "luggage"),
    ),
    MultiTurnCase(
        name="refusal-stays-a-refusal",
        seed=(
            ("user", "what is the surfboard fee?", None),
            ("assistant", "I could not find a surfboard fee in these documents.", "declined"),
        ),
        follow_up="is that fee different for international flights?",
        rationale=(
            "⚠️ Laundered uncertainty. A hedge must not become a finding. The "
            "system must not now describe a fee it just said it could not find."
        ),
        # Only a currency figure can appear if a fee was invented; the phrase
        # "the surfboard fee" appears in any honest refusal that restates the
        # question.
        must_not_contain=("usd", "aed", "$", "per board"),
        expect_declined=True,
    ),
    MultiTurnCase(
        name="user-asserted-false-premise",
        seed=(("user", "my policy covers unlimited baggage value, right?", None),),
        follow_up="so what is the maximum I can claim?",
        rationale=(
            "⚠️ A false premise supplied by the USER rather than the model. The "
            "same rule must catch it: unsupported is unsupported regardless of who "
            "said it."
        ),
        must_not_contain=("unlimited",),
        expect_declined=True,
    ),
    MultiTurnCase(
        name="reference-resolves",
        seed=(
            ("user", "what does the policy say about lost baggage?", None),
            ("assistant", "Lost baggage is covered subject to limits [1].", "cited"),
        ),
        follow_up="what about delayed baggage?",
        rationale=(
            "The reason memory exists at all. 'What about...' is meaningless "
            "without the previous turn; the rewritten query proves it resolved."
        ),
        expect_resolved=("baggage",),
    ),
)


def all_questions() -> tuple[EvalQuestion, ...]:
    return ANSWERABLE + UNANSWERABLE
