"""Read-path orchestration: the control flow between the four agents.

⚠️ The application owns branching and fan-out; the agents own only what happens
inside a single call. That split is deliberate — the loop that matters (retry) and
the branch that matters (out-of-scope short-circuit) are *this* module's, and
burying them inside an agent framework would make them unobservable.

Call counts are a design commitment, not an accident:

    out of scope        1   planner only, no retrieval at all
    simple lookup       4   planner + synthesizer + verifier + (1 free judgement)
    two sub-questions   ~7  with one retry each
    pathological       capped by the SHARED search budget

The shared budget is what prevents the pathological case. Four sub-questions
times three iterations is fifteen model calls and a response no user waits for,
so the budget is spent from one pool across all sub-questions rather than
per-sub-question.

See doc/components/07-crewai.md — the authority on read-path control flow.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.engine.query import agents, citations
from app.engine.query.agents import CallLog
from app.engine.query.corpus import get_corpus_description
from app.engine.query.retrieval import SearchFilters
from app.engine.query.search import SearchResult, search
from app.shared import tracing
from app.shared.config import Settings
from app.shared.prompts import PromptSet
from app.shared.types import Citation, Passage, Plan, Provenance

logger = logging.getLogger(__name__)

#: A citation marker: [1], [12]. Its presence is what distinguishes an answer
#: grounded in the evidence from one that declined despite having evidence.
_CITATION_MARKER = re.compile(r"\[\d+\]")

#: ⚠️ Refusal language, checked even when markers ARE present.
#:
#: An earlier fix classified a refusal as `declined` when it carried no marker.
#: That was necessary and insufficient: observed live, the synthesizer produced
#: "The passages do not provide information about Etihad's baggage policies…
#: [1][2][3]" — a refusal WITH three citations. The marker rule alone recorded
#: that as a successful cited answer, which corrupts the decline-rate metric in
#: exactly the direction that flatters the system.
#:
#: ⚠️ The SUBJECT of the negation must be the evidence, not a topic.
#:
#: An earlier, looser version matched any "does not provide", and a good
#: comparison answer regressed because of it: "[4] Delta's liability is limited
#: to $4,700... [5] Etihad Airways does not provide specific details" was
#: classified as a refusal, so four real citations rendered under the heading
#: "Closest matches — these do NOT answer the question". The answer contradicted
#: its own sources.
#:
#: "The PASSAGES do not contain X" is a refusal. "ETIHAD does not specify X" is a
#: finding about one subject inside a real answer, and must stay `cited`.
#:
#: Heuristic, and deliberately so: the alternative is another model call to
#: classify text the model just wrote.
_REFUSAL = re.compile(
    r"(?:"
    # The EVIDENCE is what fails — "the passages do not contain", "these
    # documents do not cover", "no information in the passages".
    r"(?:the |these |provided |given )?(?:passages?|documents?|information|context)"
    r"\s+(?:provided\s+|given\s+)?(?:do(?:es)? not|don't|doesn't|cannot|can't)"
    r"\s+(?:contain|provide|cover|specify|mention|include|answer|indicate)"
    # Or the assistant states it cannot answer FROM them.
    r"|(?:i )?cannot (?:determine|answer|provide|confirm)[^.]{0,60}"
    r"(?:based on|from) (?:the |these |this )?(?:passages?|documents?|information|context)"
    r"|outside what these documents cover"
    r"|not (?:covered|addressed) in (?:the|these) (?:documents?|passages?)"
    r")",
    re.IGNORECASE,
)

#: Called with a short human-readable stage description as the read path
#: progresses. Optional, and the pipeline is fully functional without it —
#: it exists so a 30-second wait can be made legible, not to drive logic.
ProgressFn = Callable[[str], None]


@dataclass
class AnswerResult:
    """Everything one question produced, including how it got there.

    The decision fields exist to go on the ROOT span. Several agentic behaviours
    can fail into inertness while still producing well-formed answers — a planner
    that always says `lookup`, a retrieval agent that never retries, a verifier
    that never retracts. Each looks healthy per-request; only the distribution
    across many requests reveals it, and that requires these being recorded.
    """

    answer: str
    passages: list[Passage]
    plan: Plan
    provenance: Provenance

    #: Renumbered by first appearance and merged on (file, page). Empty for a
    #: declined answer, because nothing supported an answer there was none of.
    citations: tuple[Citation, ...] = ()

    #: Only ever populated for a DECLINED answer, and never sources. What was
    #: found but did not answer the question, so a dead end becomes a next step.
    near_misses: tuple[Citation, ...] = ()

    #: Markers the synthesizer wrote that matched no passage it was given. A
    #: non-zero rate means references are being invented, which nothing about
    #: the answer's fluency would reveal.
    invalid_markers: tuple[int, ...] = ()

    model_calls: int = 0
    calls_by_agent: dict[str, int] = field(default_factory=dict)
    searches_used: int = 0
    retries_used: int = 0
    claims_retracted: int = 0
    claims_hedged: int = 0
    unverified: bool = False
    degradations: list[str] = field(default_factory=list)
    latency_ms: int = 0

    @property
    def declined(self) -> bool:
        return self.provenance == "declined"

    def span_attributes(self) -> dict[str, object]:
        """Flat key/values for the root span — see doc/components/08-arize-phoenix.md."""
        return {
            "intent": self.plan.intent,
            "sub_question_count": len(self.plan.sub_questions),
            "model_calls": self.model_calls,
            "searches_used": self.searches_used,
            "retries_used": self.retries_used,
            "claims_retracted": self.claims_retracted,
            "claims_hedged": self.claims_hedged,
            "unverified": self.unverified,
            "provenance": self.provenance,
            "passages_used": len(self.passages),
            "citations": len(self.citations),
            "invalid_markers": len(self.invalid_markers),
            "degraded": bool(self.degradations),
            "degradations": ",".join(self.degradations),
            "latency_ms": self.latency_ms,
        }


@dataclass
class _Budget:
    """Searches remaining, shared across every sub-question in one question."""

    remaining: int
    used: int = 0

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.used += 1
        return True


async def _retrieve_with_crew(
    session: Session,
    sub_question: str,
    *,
    settings: Settings,
    filters: SearchFilters,
    prompts: PromptSet,
    budget: _Budget,
    call_log: CallLog,
    embedding: list[float],
    keep: int,
) -> tuple[list[Passage], list[Passage], int, list[str]]:
    """⚠️ The one place the AGENT owns the loop rather than the application.

    Everywhere else the application decides what happens next. Here the retrieval
    specialist calls its single `search` tool repeatedly inside one task, reworing
    the query until the evidence satisfies it or `max_iter` stops it — which is
    exactly the split `doc/components/07-crewai.md` §4 settles on. The retry bound
    becomes a configured limit instead of hand-written loop counting.

    The shared budget still applies, because the agent cannot be trusted with it:
    the tool refuses further searches once the pool is spent and says so in words
    the agent can act on, rather than raising and discarding the work already
    done.
    """
    import anyio

    from app.engine.query import crew as crew_module

    tool = crew_module.make_search_tool(
        session,
        embedding,
        settings.retrieval,
        filters,
        keep=keep,
        budget=budget.remaining,
    )

    degradations: list[str] = []
    try:
        llm = crew_module.build_llm(settings.ollama.answering_model, settings.ollama)
        agent = crew_module.retrieval_agent(
            prompts, llm, tool, max_iter=settings.agents.retry_cap + 1
        )
        await anyio.to_thread.run_sync(
            crew_module.run_task,
            agent,
            (
                f"Find passages that answer this question: {sub_question}\n\n"
                "Use the search tool. If the results do not answer it, reword the "
                "query and search again. Stop as soon as the evidence is sufficient."
            ),
            "A short statement of whether sufficient evidence was found.",
        )
        call_log.record("retrieval-specialist")
    except Exception as exc:
        logger.warning("crew retrieval failed (%s); using whatever the tool collected", exc)
        degradations.append(f"crew-retrieval: {type(exc).__name__}")

    # Spend the shared budget by what the tool actually used, not by what the
    # agent claims it did.
    for _ in range(tool.calls):
        budget.take()

    # ⚠️ Passages come from the TOOL, never from the agent's prose. The agent
    # returns a narrative; the citation layer needs Passage objects with scores,
    # pages and section headings, and reconstructing those by parsing prose would
    # invent a fragile channel where a direct reference already exists.
    deduplicated: dict[str, Passage] = {p.chunk_id: p for p in tool.collected}
    passages = sorted(deduplicated.values(), key=lambda p: -p.score)[:keep]

    retries = max(tool.calls - 1, 0)
    return passages, [], retries, degradations


async def _retrieve_for_sub_question(
    session: Session,
    sub_question: str,
    *,
    settings: Settings,
    filters: SearchFilters,
    prompts: PromptSet,
    budget: _Budget,
    call_log: CallLog,
    embed: object,
    keep: int,
) -> tuple[list[Passage], list[Passage], int, list[str]]:
    """Search, judge, and retry a single sub-question within the shared budget.

    Returns (passages, near_misses, retries_used, degradations). The near
    misses are what a declined answer offers as "closest matches" — they are
    never sources, and exist so a refusal is a next step rather than a dead
    end.
    """
    from app.shared.models import OllamaClient

    assert isinstance(embed, OllamaClient)

    retries = 0
    degradations: list[str] = []
    best: SearchResult | None = None
    query = sub_question

    for attempt in range(1, settings.agents.retry_cap + 2):
        if not budget.take():
            logger.info("search budget exhausted; stopping retries for %r", sub_question)
            break

        embedding = await embed.embed(query)
        result = search(session, query, embedding, settings.retrieval, filters, keep=keep)
        if best is None or len(result.passages) > len(best.passages):
            best = result

        # Nothing cleared the floor -> 0.0. Judging sufficiency on an empty set
        # would be a wasted model call; the answer is self-evidently "no".
        top_score = result.passages[0].score if result.passages else 0.0

        # The score extremes are free: no model call needed to know that a 0.95
        # match is sufficient or that nothing above the floor is not.
        if top_score >= settings.retrieval.sufficient_high:
            break
        if attempt > settings.agents.retry_cap:
            break

        judgement = await agents.judge_sufficiency(
            query,
            result.passages,
            attempt=attempt,
            max_attempts=settings.agents.retry_cap + 1,
            prompts=prompts,
            settings=settings.ollama,
            call_log=call_log,
        )
        if judgement.degraded and judgement.reason:
            degradations.append(judgement.reason)
        if judgement.value.sufficient or not judgement.value.new_query:
            break

        query = judgement.value.new_query
        retries += 1

    return (
        best.passages if best else [],
        best.near_misses if best else [],
        retries,
        degradations,
    )


def _allocate_passages(
    per_sub_question: list[list[Passage]], pooled: list[Passage], *, keep: int
) -> list[Passage]:
    """Choose the passages the synthesizer sees, fairly across sub-questions.

    ⚠️ A single global ranking cannot express a comparison, and this was observed
    live rather than reasoned about.

    Asked "whose baggage services are better, Etihad or Delta?", the planner
    correctly produced one sub-question per airline and retrieval correctly found
    passages for both. Then every slot went to Delta, because Delta's contract
    states baggage rules in one dense clause that scores higher than Etihad's
    spread across several. The synthesizer received five Delta passages, no
    Etihad passages, and truthfully reported that it could not compare.

    Nothing was broken: the planner decomposed, retrieval retrieved, the
    synthesizer refused honestly for lack of evidence. The failure lived entirely
    in the *merge*, where the losing side is silently discarded.

    So each sub-question is guaranteed a share of the budget first, and only the
    remainder is filled by global score. With one sub-question this is exactly
    the old behaviour.
    """
    seen: set[str] = set()
    chosen: list[Passage] = []

    def take(passage: Passage) -> bool:
        if passage.chunk_id in seen or len(chosen) >= keep:
            return False
        seen.add(passage.chunk_id)
        chosen.append(passage)
        return True

    groups = [g for g in per_sub_question if g]
    if len(groups) > 1:
        # At least one each, so a sub-question is never wholly unrepresented.
        share = max(1, keep // len(groups))
        for group in groups:
            for passage in sorted(group, key=lambda p: -p.score)[:share]:
                take(passage)

    # Remaining slots by global score — this is the whole selection when there is
    # only one sub-question.
    for passage in sorted(pooled, key=lambda p: -p.score):
        take(passage)

    # Present in relevance order regardless of how each was chosen.
    chosen.sort(key=lambda p: -p.score)
    return chosen


async def answer_question(
    session: Session,
    question: str,
    *,
    settings: Settings,
    prompts: PromptSet,
    collection: str,
    conversation_summary: str = "",
    recent_turns: str = "",
    keep: int | None = None,
    on_progress: ProgressFn | None = None,
) -> AnswerResult:
    """The whole read path for one question.

    The entire body runs inside ONE root span, and every decision attribute is
    stamped on it before returning. Attributes on a child span would be invisible
    to the aggregate queries that detect inert agents — a planner stuck on
    `lookup`, a retry loop that never fires — which is the only way those failures
    surface at all. See doc/components/08-arize-phoenix.md.
    """
    from app.shared.models import OllamaClient

    started = time.monotonic()
    keep = keep if keep is not None else settings.retrieval.keep_quality
    filters = SearchFilters(collection=collection)
    call_log = CallLog()
    degradations: list[str] = []

    def progress(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    corpus = get_corpus_description(session, collection)
    progress("Planning the search")

    plan_fn = agents.plan_with_crew if settings.agents.crewai_available else agents.plan
    planned = await plan_fn(
        question,
        corpus_description=corpus.text or "(corpus contents unknown)",
        conversation_summary=conversation_summary or "(none)",
        recent_turns=recent_turns or "(none)",
        prompts=prompts,
        settings=settings.ollama,
        sub_question_cap=settings.agents.sub_question_cap,
        call_log=call_log,
    )
    if planned.degraded and planned.reason:
        degradations.append(planned.reason)
    plan = planned.value

    # ---- the short-circuit: out of scope costs ONE call, not four ------------
    # ---- the second short-circuit: "what can I ask you?" ---------------------
    #
    # ⚠️ A question about the SYSTEM is not a question for the documents.
    #
    # Without this branch, "what type of questions can I ask you?" was planned as
    # an ordinary lookup, searched against a corpus of baggage rules, found
    # nothing above the floor, and came back after **five minutes** with a
    # refusal and three irrelevant "closest matches". Every stage behaved
    # correctly; the question was simply never a retrieval question.
    #
    # The answer is already in hand — the corpus description is loaded on every
    # request so the planner can judge scope — so this costs no search and no
    # further model call.
    if plan.intent == "capability":
        progress("Describing what these documents cover")
        overview = corpus.text or "(no documents are indexed yet)"
        described = AnswerResult(
            answer=(
                "I answer questions from a fixed set of documents, quoting the "
                "passage each claim comes from.\n\n"
                f"{overview}\n\n"
                "Ask about anything above and I will cite the page. If the "
                "documents do not cover it, I will say so rather than guess."
            ),
            passages=[],
            plan=plan,
            # Not "cited": nothing was retrieved, so there is nothing to cite.
            # Calling it cited would corrupt the same decline-rate metric the
            # citation-marker rule exists to protect.
            provenance="declined",
            model_calls=call_log.calls,
            calls_by_agent=dict(call_log.by_agent),
            degradations=degradations,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        tracing.set_root_attributes(**described.span_attributes())
        return described

    if plan.intent == "out_of_scope":
        progress("The question is outside what these documents cover")
        declined = AnswerResult(
            answer=(
                "That question is outside what these documents cover, so I cannot "
                "answer it from them."
            ),
            passages=[],
            plan=plan,
            provenance="declined",
            model_calls=call_log.calls,
            calls_by_agent=dict(call_log.by_agent),
            degradations=degradations,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        tracing.set_root_attributes(**declined.span_attributes())
        return declined

    budget = _Budget(remaining=settings.agents.search_budget)
    client = OllamaClient(settings.ollama)

    try:
        collected: list[Passage] = []
        # ⚠️ Kept GROUPED as well as pooled. A comparison cannot survive a single
        # global ranking — see the allocation below.
        per_sub_question: list[list[Passage]] = []
        collected_near: list[Passage] = []
        total_retries = 0
        for sub_question in plan.sub_questions:
            progress(f"Searching: {sub_question}")
            if settings.agents.crewai_available:
                embedding = await client.embed(sub_question)
                (
                    sub_passages,
                    sub_near,
                    retries,
                    sub_degradations,
                ) = await _retrieve_with_crew(
                    session,
                    sub_question,
                    settings=settings,
                    filters=filters,
                    prompts=prompts,
                    budget=budget,
                    call_log=call_log,
                    embedding=embedding,
                    keep=keep,
                )
            else:
                (
                    sub_passages,
                    sub_near,
                    retries,
                    sub_degradations,
                ) = await _retrieve_for_sub_question(
                    session,
                    sub_question,
                    settings=settings,
                    filters=filters,
                    prompts=prompts,
                    budget=budget,
                    call_log=call_log,
                    embed=client,
                    keep=keep,
                )
            collected.extend(sub_passages)
            per_sub_question.append(sub_passages)
            collected_near.extend(sub_near)
            total_retries += retries
            degradations.extend(sub_degradations)
    finally:
        await client.aclose()

    # Deduplicate across sub-questions by chunk id, BEFORE numbering — the
    # synthesizer must never see the same passage twice under two numbers.
    # (The second deduplication, by (file, page), happens after verification in
    # the citation layer — different key, different purpose.)
    passages = _allocate_passages(per_sub_question, collected, keep=keep)

    progress(f"Found {len(passages)} passage(s); composing the answer")
    synthesize_fn = (
        agents.synthesize_with_crew if settings.agents.crewai_available else agents.synthesize
    )
    drafted = await synthesize_fn(
        plan.standalone_question,
        passages,
        conversation_summary=conversation_summary or "(none)",
        prompts=prompts,
        settings=settings.ollama,
        call_log=call_log,
    )
    if drafted.degraded and drafted.reason:
        degradations.append(drafted.reason)

    progress("Checking every claim against the sources")
    verify_fn = agents.verify_with_crew if settings.agents.crewai_available else agents.verify
    verified = await verify_fn(
        drafted.value,
        passages,
        prompts=prompts,
        settings=settings.ollama,
        call_log=call_log,
    )
    if verified.degraded and verified.reason:
        degradations.append(verified.reason)

    # ⚠️ Provenance is derived from the ANSWER, not from whether passages exist.
    #
    # An earlier version checked only `if not passages` — so an answer that
    # explicitly refused ("The passages provided do not specify...") while
    # holding 5 passages was still recorded as "cited". That is exactly the
    # mislabelling that would corrupt the decline-rate metric requirement 2.1 is
    # measured by: a refusal counted as a citation.
    #
    # A genuine citation must carry at least one [n] marker. No marker means
    # nothing was actually cited, whatever the passage count says.
    provenance: Provenance
    answer_text = verified.value.verified_answer
    has_citation_marker = bool(_CITATION_MARKER.search(answer_text))
    # ⚠️ A refusal is a refusal even when it cites. See _REFUSAL above.
    reads_as_refusal = bool(_REFUSAL.search(answer_text))
    if not passages or not has_citation_marker or reads_as_refusal:
        provenance = "declined"
    elif verified.value.claims_retracted or verified.value.claims_hedged:
        provenance = "hedged"
    else:
        provenance = "cited"

    # ---- markers become references the reader can act on ---------------------
    #
    # Done AFTER verification, deliberately. The verifier retracts unsupported
    # claims, and a retracted claim takes its marker with it — so building the
    # source list from the draft would list sources for sentences that are no
    # longer in the answer.
    if provenance == "declined":
        # No sources, because nothing supported an answer. What was found is
        # offered separately and must never be rendered as support.
        cited = citations.CitedAnswer(answer=verified.value.verified_answer, citations=())
        # Passages that cleared the floor but went uncited come first: they were
        # the closest thing to an answer. Otherwise fall back to what the floor
        # rejected, which is the usual case when nothing cleared it at all.
        near = citations.near_misses(passages or sorted(collected_near, key=lambda p: -p.score))
    else:
        cited = citations.build(verified.value.verified_answer, passages)
        near = ()

    result = AnswerResult(
        answer=cited.answer,
        citations=cited.citations,
        near_misses=near,
        invalid_markers=cited.invalid_markers,
        passages=passages,
        plan=plan,
        provenance=provenance,
        model_calls=call_log.calls,
        calls_by_agent=dict(call_log.by_agent),
        searches_used=budget.used,
        retries_used=total_retries,
        claims_retracted=verified.value.claims_retracted,
        claims_hedged=verified.value.claims_hedged,
        unverified=verified.value.unverified,
        degradations=degradations,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    tracing.set_root_attributes(**result.span_attributes())
    return result
