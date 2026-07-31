"""The custom harness: the obligations RAGAs does not cover.

⚠️ **RAGAs measures roughly half of what has to be measured here.** It scores
faithfulness, answer relevancy and context precision/recall — all about answers
that were *given*. It says nothing about:

  · whether the system DECLINED when it should have
  · whether citation markers resolve to real passages
  · whether the agents are making decisions at all
  · latency, cache effectiveness, retrieval recall

Treating RAGAs as the whole answer is the most common way this requirement is
under-delivered, so this module exists alongside it rather than after it.

⚠️ **The inert-agent detector is the most important thing here**, and it is the
one metric that fails a run rather than merely reporting a number.

A planner that always returns `lookup`. A retrieval agent that never retries. A
verifier that never retracts a claim. Each produces well-formed, fluent, cited
output. Each passes every per-request check. **Only the distribution across many
requests reveals it** — and only if something asserts on it. A constant is the
signature of an agent that has stopped deciding and is merely echoing.
"""

from __future__ import annotations

import statistics
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.engine.query.pipeline import AnswerResult, answer_question
from app.shared.config import Settings
from app.shared.prompts import PromptSet
from eval.datasets import ANSWERABLE, MULTI_TURN, UNANSWERABLE, EvalQuestion


@dataclass
class QuestionOutcome:
    """One question's result, flattened for reporting."""

    question: str
    kind: str
    intent: str
    provenance: str
    citations: int
    passages: int
    invalid_markers: int
    model_calls: int
    searches: int
    retries: int
    claims_retracted: int
    latency_s: float
    answer: str
    degraded: bool

    @property
    def declined(self) -> bool:
        return self.provenance == "declined"


@dataclass
class Report:
    outcomes: list[QuestionOutcome] = field(default_factory=list)
    multi_turn: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ decline rate

    def decline_rate(self, kind: str) -> float:
        """Proportion of a question kind the system refused to answer.

        Read as two numbers, never one. On `answerable` a high rate means the
        system is uselessly timid; on `out_of_scope` and `near_miss` a low rate
        means it is confabulating. A single averaged figure hides both.
        """
        subset = [o for o in self.outcomes if o.kind == kind]
        if not subset:
            return 0.0
        return sum(1 for o in subset if o.declined) / len(subset)

    # -------------------------------------------------------- citation validity

    def citation_validity(self) -> dict[str, Any]:
        """Every marker must resolve to a passage the synthesizer was given.

        `invalid_markers` counts the ones that did not. A non-zero total means
        references are being invented — which nothing about an answer's fluency
        would reveal, because a fabricated `[7]` renders exactly like a real one.
        """
        cited = [o for o in self.outcomes if not o.declined]
        return {
            "answers_with_citations": len(cited),
            "invalid_markers_total": sum(o.invalid_markers for o in self.outcomes),
            "answers_cited_without_passages": sum(
                1 for o in cited if o.citations > 0 and o.passages == 0
            ),
        }

    # --------------------------------------------------- ⚠️ inert-agent detector

    def decision_distributions(self) -> dict[str, dict[str, int]]:
        return {
            "intent": dict(Counter(o.intent for o in self.outcomes)),
            "provenance": dict(Counter(o.provenance for o in self.outcomes)),
            "retries": dict(Counter(str(o.retries) for o in self.outcomes)),
            "claims_retracted": dict(Counter(str(o.claims_retracted) for o in self.outcomes)),
        }

    def assert_agents_are_deciding(self) -> list[str]:
        """⚠️ FAIL the run when any decision is constant.

        This is the only detector for an agent that has stopped deciding. It is
        deliberately an assertion rather than a reported number: a metric nobody
        reads is exactly how an inert planner survives to a demonstration.

        The corpus and question set are chosen so that variety is *expected* —
        answerable and unanswerable questions, near misses that should retry.
        A constant here is therefore evidence, not noise.
        """
        problems: list[str] = []
        distributions = self.decision_distributions()

        for name, counts in distributions.items():
            if len(counts) <= 1 and len(self.outcomes) > 1:
                only = next(iter(counts), "nothing")
                problems.append(
                    f"INERT: every question produced {name}={only!r} across "
                    f"{len(self.outcomes)} questions — the agent that decides "
                    f"{name} is not deciding anything"
                )

        # A planner that never short-circuits is inert even if `intent` varies
        # among answerable labels.
        intents = distributions["intent"]
        if "out_of_scope" not in intents and any(o.kind == "out_of_scope" for o in self.outcomes):
            problems.append(
                "INERT: no question was classified out_of_scope, though the set "
                "contains questions that are plainly outside the corpus"
            )

        return problems

    # ----------------------------------------------------------------- latency

    def latency_percentiles(self) -> dict[str, float]:
        values = sorted(o.latency_s for o in self.outcomes)
        if not values:
            return {}

        def pct(p: float) -> float:
            index = min(int(len(values) * p), len(values) - 1)
            return round(values[index], 1)

        return {
            "p50": pct(0.50),
            "p90": pct(0.90),
            "max": round(values[-1], 1),
            "mean": round(statistics.mean(values), 1),
        }

    # ------------------------------------------------------------------- cost

    def call_efficiency(self) -> dict[str, float]:
        """Model calls by question kind.

        The out-of-scope short-circuit is a design commitment — one call, no
        search. If this creeps up, the branch has been lost, and no answer would
        look wrong.
        """
        by_kind: dict[str, list[int]] = {}
        for outcome in self.outcomes:
            by_kind.setdefault(outcome.kind, []).append(outcome.model_calls)
        return {k: round(statistics.mean(v), 2) for k, v in by_kind.items()}


async def run_question(
    session: Session,
    item: EvalQuestion,
    *,
    settings: Settings,
    prompts: PromptSet,
    collection: str,
) -> QuestionOutcome:
    started = time.monotonic()
    result: AnswerResult = await answer_question(
        session,
        item.question,
        settings=settings,
        prompts=prompts,
        collection=collection,
    )
    return QuestionOutcome(
        question=item.question,
        kind=item.kind,
        intent=result.plan.intent,
        provenance=result.provenance,
        citations=len(result.citations),
        passages=len(result.passages),
        invalid_markers=len(result.invalid_markers),
        model_calls=result.model_calls,
        searches=result.searches_used,
        retries=result.retries_used,
        claims_retracted=result.claims_retracted,
        latency_s=round(time.monotonic() - started, 1),
        answer=result.answer,
        degraded=bool(result.degradations),
    )


async def run_multi_turn(
    session: Session, *, settings: Settings, prompts: PromptSet, collection: str
) -> list[dict[str, Any]]:
    """⚠️ The only construction that detects memory poisoning.

    Single-turn evaluation cannot find it: every turn judged alone is fluent,
    cited and verified. The failure is that turn 3's hedge becomes turn 5's
    premise, so the history has to be seeded deliberately.
    """
    import uuid

    from app.engine.query.conversation import answer_turn
    from app.shared.store import repository
    from app.shared.types import Turn

    results: list[dict[str, Any]] = []
    for case in MULTI_TURN:
        conversation_id = str(uuid.uuid4())
        repository.get_or_create_conversation(session, conversation_id)
        for index, (role, content, provenance) in enumerate(case.seed):
            repository.write_turn(
                session,
                Turn(
                    id="",
                    conversation_id=conversation_id,
                    turn_index=index,
                    role=role,  # type: ignore[arg-type]
                    content=content,
                    provenance=provenance,  # type: ignore[arg-type]
                ),
            )
        session.commit()

        turn = await answer_turn(
            session,
            conversation_id,
            case.follow_up,
            settings=settings,
            prompts=prompts,
            collection=collection,
        )
        answer = turn.answer.answer.lower()
        rewritten = turn.answer.plan.standalone_question.lower()

        leaked = [s for s in case.must_not_contain if s.lower() in answer]
        # ANY of the alternatives is enough — see EvalQuestion.expect_resolved.
        resolved = not case.expect_resolved or any(
            s.lower() in rewritten for s in case.expect_resolved
        )
        unresolved = [] if resolved else list(case.expect_resolved)
        wrongly_answered = case.expect_declined and turn.answer.provenance != "declined"

        results.append(
            {
                "case": case.name,
                "rationale": case.rationale,
                "passed": not leaked and not unresolved and not wrongly_answered,
                "wrongly_answered": wrongly_answered,
                "leaked": leaked,
                "unresolved_references": unresolved,
                "provenance": turn.answer.provenance,
                "rewritten": turn.answer.plan.standalone_question,
                "answer": turn.answer.answer[:300],
            }
        )
    return results


async def evaluate(
    session: Session, *, settings: Settings, prompts: PromptSet, collection: str
) -> Report:
    report = Report()

    for item in ANSWERABLE + UNANSWERABLE:
        report.outcomes.append(
            await run_question(
                session, item, settings=settings, prompts=prompts, collection=collection
            )
        )

    report.multi_turn = await run_multi_turn(
        session, settings=settings, prompts=prompts, collection=collection
    )

    # ⚠️ Assertions run LAST and are what makes this a gate rather than a report.
    report.failures.extend(report.assert_agents_are_deciding())

    for case in report.multi_turn:
        if not case["passed"]:
            report.failures.append(
                f"MEMORY: {case['case']} — leaked={case['leaked']} "
                f"unresolved={case['unresolved_references']}"
            )

    invalid = report.citation_validity()["invalid_markers_total"]
    if invalid:
        report.failures.append(
            f"CITATIONS: {invalid} marker(s) pointed at no passage — references are being invented"
        )

    return report
