"""Read-path control flow, with every model call and every search faked.

⚠️ The call counts asserted here are a *design commitment*, not an observation.
The whole argument for an agentic read path is that it spends effort in
proportion to the question — one call to refuse an out-of-scope question, a
bounded number to answer a hard one. If those numbers drift, the design has
quietly stopped being true while every answer still looks fine.

Nothing here touches a model or a database, so the counts are exact rather than
approximate. The live equivalents are recorded in
doc/implementation/06-agentic-read-path.md §2.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.engine.query import agents, pipeline
from app.engine.query.corpus import CorpusDescription
from app.engine.query.search import SearchResult
from app.shared.config import Settings
from app.shared.prompts import PromptSet, ResolvedPrompt
from app.shared.types import Passage

# Every prompt renders to its own name plus the variables it was given. The
# agents only need *something* to send; what matters is how many times they send.
_PROMPT_NAMES = ("planner", "retrieval-specialist", "synthesizer", "verifier")


@pytest.fixture
def prompts() -> PromptSet:
    # The template is just the agent's own name, which is how the recorder below
    # tells the four agents apart without depending on real prompt wording.
    return PromptSet(
        {
            name: ResolvedPrompt(name=name, version="test", template=name, source="bundled")
            for name in _PROMPT_NAMES
        }
    )


class _FakeClient:
    """Stands in for OllamaClient. Embedding is not what this file measures."""

    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def embed(self, _text: str) -> list[float]:
        return [0.0]

    async def aclose(self) -> None:
        return None


def _passage(chunk_id: str, score: float) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id="d1",
        source_file="f.pdf",
        title="T",
        page=1,
        display_text="Excess baggage is charged per the applicable tariff.",
        score=score,
    )


class _Recorder:
    """Captures each prompt sent, and replies with whatever the test scripted."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.prompts_sent: list[str] = []

    async def __call__(self, prompt_text: str, model: str, settings: Any, **_: Any) -> str:
        self.prompts_sent.append(prompt_text)
        for key, reply in self.replies.items():
            if key in prompt_text:
                return reply
        raise AssertionError(f"unscripted agent call: {prompt_text[:120]!r}")


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch out the corpus lookup, the search, and the model client."""

    def _install(replies: dict[str, str], *, top_score: float = 0.95) -> _Recorder:
        recorder = _Recorder(replies)
        monkeypatch.setattr(agents, "_generate", recorder)
        monkeypatch.setattr(
            pipeline,
            "get_corpus_description",
            lambda *_a, **_k: CorpusDescription(
                collection="c", text="This corpus covers: baggage rules.", document_count=1
            ),
        )
        monkeypatch.setattr(
            pipeline,
            "search",
            lambda *_a, **_k: SearchResult(passages=[_passage("c1", top_score)]),
        )
        monkeypatch.setattr("app.shared.models.OllamaClient", _FakeClient)
        return recorder

    return _install


def _plan(intent: str, subs: list[str]) -> str:
    return json.dumps({"intent": intent, "standalone_question": "q?", "sub_questions": subs})


def _settings() -> Settings:
    """⚠️ Pinned to the DIRECT path, not the Crew.AI one.

    These tests patch `agents._generate`, which is how the direct path reaches a
    model. The crew path goes through Crew.AI instead, so the same patch would
    let real network calls escape — and the exact call counts asserted below are
    path-specific anyway (the crew always spends a call on the retrieval
    specialist; the direct path only judges sufficiency when the score is
    ambiguous).

    The application-owned control flow being asserted — the short-circuits, the
    shared budget, the degradation paths — is identical in both. What differs is
    who talks to the model, which is exactly what `test_crew_control_flow.py`
    covers separately.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings.agents.use_crewai = False
    return settings


async def _run(question: str, prompts: PromptSet, **kwargs: Any) -> pipeline.AnswerResult:
    return await pipeline.answer_question(
        None,  # type: ignore[arg-type]  # every DB call is patched out
        question,
        settings=_settings(),
        prompts=prompts,
        collection="c",
        **kwargs,
    )


# ------------------------------------------------------- the short-circuit


async def test_out_of_scope_costs_exactly_one_call_and_no_search(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ The cheapest path in the system, and the one most easily lost.

    An out-of-scope question must not reach the database at all. If a refactor
    lets it fall through to retrieval, every answer still looks right and the
    cost of refusing silently quadruples.
    """
    recorder = wired({"planner": _plan("out_of_scope", [])})

    result = await _run("how do I bake sourdough bread?", prompts)

    assert result.model_calls == 1
    assert result.searches_used == 0
    assert result.passages == []
    assert result.provenance == "declined"
    assert len(recorder.prompts_sent) == 1


# --------------------------------------------------------- the budget cap


async def test_the_search_budget_is_shared_across_sub_questions(
    wired: Any, prompts: PromptSet
) -> None:
    """Four sub-questions each retrying twice would be far more searches than
    any user waits for. The budget is one pool for the whole question, so the
    total is bounded by the budget and NOT by (sub-questions x retries)."""
    settings = _settings()
    wired(
        {
            "planner": _plan("comparison", ["a?", "b?", "c?", "d?"]),
            # Never sufficient, always a new query — the pathological case.
            "retrieval-specialist": json.dumps({"sufficient": False, "new_query": "again?"}),
            "synthesizer": "An answer [1].",
            "verifier": json.dumps({"verified_answer": "An answer [1].", "claims_retracted": 0}),
        },
        top_score=0.1,  # below sufficient_high, so the retry loop always engages
    )

    result = await _run("compare everything", prompts)

    assert result.searches_used <= settings.agents.search_budget
    assert result.searches_used == settings.agents.search_budget, (
        "the pathological case should exhaust the budget, proving the cap is what stopped it"
    )


# ------------------------------------------------------------ degradation


async def test_an_unparseable_plan_degrades_to_a_single_lookup(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ Fail-forward is only acceptable when the failure is recorded.

    doc/02-architecture.md §7 is explicit that degrading silently makes failures
    invisible. The answer is allowed to be produced; what is not allowed is
    producing it without saying the planner failed.
    """
    wired(
        {
            "planner": "I could not determine a plan.",
            "synthesizer": "An answer [1].",
            "verifier": json.dumps({"verified_answer": "An answer [1].", "claims_retracted": 0}),
        }
    )

    result = await _run("what is the excess baggage charge?", prompts)

    assert result.plan.intent == "lookup"
    assert result.degradations, "the planner failure must be recorded, not swallowed"
    assert any("planner" in reason for reason in result.degradations)
    assert result.provenance == "cited"


async def test_a_failed_verifier_marks_the_answer_in_its_own_text(
    wired: Any, prompts: PromptSet
) -> None:
    """Marked in the ANSWER, not only on the span. A reader must never be shown
    an unverified answer that looks identical to a verified one."""
    wired(
        {
            "planner": _plan("lookup", ["what is the charge?"]),
            "synthesizer": "An answer [1].",
            "verifier": "the verifier returned prose instead of JSON",
        }
    )

    result = await _run("what is the charge?", prompts)

    assert result.unverified
    assert "could not be verified" in result.answer
    assert any("verifier" in reason for reason in result.degradations)


# ----------------------------------------------------------- provenance


async def test_a_refusal_holding_passages_is_declined_not_cited(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ The bug this rule exists for: an answer that explicitly refused while
    holding five passages was recorded as `cited`, which is exactly the
    mislabelling that would corrupt the decline-rate metric."""
    refusal = "The passages provided do not specify the charge."
    wired(
        {
            "planner": _plan("lookup", ["what is the charge?"]),
            "synthesizer": refusal,
            "verifier": json.dumps({"verified_answer": refusal, "claims_retracted": 0}),
        }
    )

    result = await _run("what is the charge?", prompts)

    assert result.passages, "the refusal is only interesting because evidence WAS retrieved"
    assert result.provenance == "declined"


async def test_a_refusal_that_cites_is_still_declined(wired: Any, prompts: PromptSet) -> None:
    """⚠️ Observed live, and the second half of a bug I only half-fixed.

    The earlier rule was "no [n] marker means declined", which caught refusals
    that cited nothing. It missed the shape that actually occurred:

        "The passages do not provide information about Etihad's baggage
         policies. Therefore I cannot determine... [1][2][3]"

    A refusal carrying three citations. The marker rule recorded that as a
    successful cited answer — inflating the decline-rate metric in precisely the
    direction that flatters the system, which is the direction nobody questions.
    """
    refusal = "The passages do not provide information about that. [1][2]"
    wired(
        {
            "planner": _plan("lookup", ["what is the charge?"]),
            "synthesizer": refusal,
            "verifier": json.dumps({"verified_answer": refusal, "claims_retracted": 0}),
        }
    )

    result = await _run("what is the charge?", prompts)

    assert result.passages, "the point is that evidence WAS retrieved"
    assert result.provenance == "declined"


async def test_a_genuine_answer_with_hedging_language_stays_cited(
    wired: Any, prompts: PromptSet
) -> None:
    """The refusal test must not swallow real answers.

    "Subject to limits" and "does not apply where..." are ordinary policy
    wording. A broad refusal pattern would classify most correct insurance
    answers as declines — the opposite failure, and just as damaging.
    """
    answer = (
        "You are covered for lost baggage, subject to limits [1]. Cover does not apply to cash [2]."
    )
    wired(
        {
            "planner": _plan("lookup", ["am I covered?"]),
            "synthesizer": answer,
            "verifier": json.dumps({"verified_answer": answer, "claims_retracted": 0}),
        }
    )

    result = await _run("am I covered?", prompts)

    assert result.provenance == "cited"


# ------------------------------------------------- comparison fairness


def _p(chunk_id: str, source: str, score: float) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id="d",
        source_file=source,
        title=source,
        page=1,
        display_text="text",
        score=score,
    )


def test_each_sub_question_is_guaranteed_a_share_of_the_budget() -> None:
    """⚠️ A comparison cannot survive a single global ranking.

    Observed live: asked which airline's baggage service was better, the planner
    correctly produced one sub-question per airline and retrieval found passages
    for both — then every slot went to Delta, whose contract states its rules in
    one dense high-scoring clause. The synthesizer saw five Delta passages, no
    Etihad passages, and truthfully said it could not compare.

    Nothing upstream was broken. The failure was entirely in the merge.
    """
    etihad = [_p("e1", "etihad.pdf", 0.40), _p("e2", "etihad.pdf", 0.38)]
    delta = [_p(f"d{i}", "delta.pdf", 0.90 - i * 0.01) for i in range(5)]

    chosen = pipeline._allocate_passages([etihad, delta], etihad + delta, keep=5)

    sources = {p.source_file for p in chosen}
    assert "etihad.pdf" in sources, "the lower-scoring side must not be erased"
    assert "delta.pdf" in sources
    assert len(chosen) == 5


def test_a_single_sub_question_keeps_pure_global_ranking() -> None:
    """The guarantee must not perturb the ordinary case: one sub-question means
    the best passages win outright, exactly as before."""
    only = [_p(f"c{i}", "a.pdf", 0.9 - i * 0.1) for i in range(6)]

    chosen = pipeline._allocate_passages([only], only, keep=3)

    assert [p.chunk_id for p in chosen] == ["c0", "c1", "c2"]


def test_allocation_never_duplicates_or_exceeds_the_budget() -> None:
    group = [_p("x1", "a.pdf", 0.9), _p("x2", "a.pdf", 0.8)]
    chosen = pipeline._allocate_passages([group, group], group + group, keep=5)

    assert len({p.chunk_id for p in chosen}) == len(chosen)
    assert len(chosen) <= 5


async def test_a_comparison_noting_one_side_lacks_detail_stays_cited(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ A regression caused by the refusal rule itself, seen live.

    A good comparison answer says what one subject specifies AND that the other
    does not. The first version of the refusal pattern matched any "does not
    provide", so this:

        "[4] Delta's liability is limited to $4,700...
         [5] Etihad Airways does not provide specific details."

    was filed as a refusal — and its four real citations rendered under the
    heading "Closest matches — these do NOT answer the question". The answer
    contradicted its own sources on screen.

    "The PASSAGES do not contain X" is a refusal. "ETIHAD does not specify X" is
    a finding inside a real answer.
    """
    answer = (
        "[1] Delta limits liability to $4,700. "
        "[2] Etihad Airways does not provide specific details about liability."
    )
    wired(
        {
            "planner": _plan("comparison", ["delta liability?", "etihad liability?"]),
            "synthesizer": answer,
            "verifier": json.dumps({"verified_answer": answer, "claims_retracted": 0}),
        }
    )

    result = await _run("compare their liability", prompts)

    assert result.provenance == "cited"
    assert result.near_misses == (), "a cited answer must never show 'closest matches'"
