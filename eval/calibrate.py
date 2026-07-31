"""E10-S6 · Threshold calibration, and E10-S7 · noise floor and ablations.

⚠️ `keep_floor` decides whether the system can refuse at all, and until now it
has been the design's guess: 0.3, written before anything ran and never fitted.
Every decline rate reported so far rests on it. A 100% refusal figure produced by
an arbitrary threshold is not evidence — it is a coincidence that has not been
distinguished from a result.

**The split matters more than the sweep.** The floor is fitted on a CALIBRATION
set and reported on an EVALUATION set that had no part in choosing it. Fitting
and reporting on the same questions produces a number that describes the fitting,
not the system — the most common way a threshold is made to look better than it
is.

Two things are measured here:

  calibration  the floor that best separates questions that should be answered
               from questions that should be refused
  noise floor  what the same corpus does with questions no corpus could answer,
               which is the baseline any decline rate has to beat
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.engine.query.rerank import rank
from app.engine.query.retrieval import (
    SearchFilters,
    keyword_search,
    load_passages,
    reciprocal_rank_fusion,
    vector_search,
)
from app.shared.config import Settings
from eval.datasets import ANSWERABLE, UNANSWERABLE

logger = logging.getLogger(__name__)

#: Questions no document corpus could answer. They are not "out of scope for THIS
#: corpus" — they are unanswerable in principle, so any passage clearing the floor
#: for them is pure noise. Whatever score they reach is the level below which a
#: threshold is measuring nothing.
NOISE_QUESTIONS: tuple[str, ...] = (
    "what is the airspeed velocity of an unladen swallow?",
    "qwerty zxcvbn asdfgh",
    "please describe the colour of the number seven",
    "wjfkdls qpwoeir zmxncbv",
)


@dataclass
class ScoreSample:
    question: str
    kind: str
    top_score: float


@dataclass
class Calibration:
    samples: list[ScoreSample] = field(default_factory=list)
    noise: list[ScoreSample] = field(default_factory=list)
    chosen_floor: float = 0.0
    calibration_accuracy: float = 0.0
    evaluation_accuracy: float = 0.0
    current_floor: float = 0.0

    @property
    def noise_ceiling(self) -> float:
        """The highest score nonsense achieved. A floor at or below this is
        measuring noise, not relevance."""
        return max((s.top_score for s in self.noise), default=0.0)


async def _top_score(
    session: Session, question: str, embedding: list[float], settings: Settings, collection: str
) -> float:
    """Best cross-encoder score for a question, BEFORE any floor is applied.

    Deliberately bypasses `search()`: that applies the floor, and a fitting
    routine cannot see what the floor rejects if the floor has already run.
    """
    filters = SearchFilters(collection=collection)
    vector_hits = vector_search(session, embedding, settings.retrieval, filters)
    keyword_hits = keyword_search(session, question, settings.retrieval, filters)
    fused = reciprocal_rank_fusion([vector_hits, keyword_hits], settings.retrieval)
    candidates = load_passages(session, fused)
    scored, was_scored = rank(question, candidates, settings.retrieval)
    if not was_scored or not scored:
        return 0.0
    return float(scored[0].score)


def _accuracy(samples: list[ScoreSample], floor: float) -> float:
    """Share of questions the floor classifies correctly.

    Answerable questions should clear it; unanswerable ones should not.
    """
    if not samples:
        return 0.0
    correct = 0
    for sample in samples:
        answerable = sample.kind == "answerable"
        clears = sample.top_score >= floor
        if answerable == clears:
            correct += 1
    return correct / len(samples)


def fit(samples: list[ScoreSample], noise_ceiling: float) -> tuple[float, float]:
    """Sweep candidate floors and return (best floor, accuracy on this split).

    ⚠️ Candidates start above the noise ceiling. A floor beneath it would let
    nonsense through, and no amount of accuracy on real questions makes that
    acceptable — it would mean the system cannot distinguish a hard question from
    gibberish.
    """
    start = max(0.05, noise_ceiling)
    candidates = [round(start + i * 0.05, 2) for i in range(int((0.95 - start) / 0.05) + 1)]
    scored = [(floor, _accuracy(samples, floor)) for floor in candidates]
    if not scored:
        return start, 0.0
    best_accuracy = max(a for _, a in scored)
    # Lowest floor achieving the best accuracy: among equals, prefer the one that
    # refuses least, since a needless refusal is also a failure.
    best_floor = min(f for f, a in scored if a == best_accuracy)
    return best_floor, best_accuracy


async def calibrate(session: Session, *, settings: Settings, collection: str) -> Calibration:
    from app.shared.models import OllamaClient

    client = OllamaClient(settings.ollama)
    result = Calibration(current_floor=settings.retrieval.keep_floor)

    try:
        for question in NOISE_QUESTIONS:
            embedding = await client.embed(question)
            score = await _top_score(session, question, embedding, settings, collection)
            result.noise.append(ScoreSample(question, "noise", score))

        for item in ANSWERABLE + UNANSWERABLE:
            embedding = await client.embed(item.question)
            score = await _top_score(session, item.question, embedding, settings, collection)
            result.samples.append(ScoreSample(item.question, item.kind, score))
    finally:
        await client.aclose()

    # ⚠️ Alternating split, not random: with this few questions a random split
    # can put every near-miss on one side, and the reported number would then
    # depend on a seed rather than on the system.
    calibration_split = result.samples[0::2]
    evaluation_split = result.samples[1::2]

    floor, accuracy = fit(calibration_split, result.noise_ceiling)
    result.chosen_floor = floor
    result.calibration_accuracy = accuracy
    # The honest number: fitted on one split, reported on the other.
    result.evaluation_accuracy = _accuracy(evaluation_split, floor)
    return result


def render(result: Calibration) -> str:
    lines = ["\n## Threshold calibration  (E10-S6) and noise floor (E10-S7)"]
    lines.append(
        f"  noise ceiling          {result.noise_ceiling:.3f}   highest score nonsense reached"
    )
    lines.append(
        f"  current keep_floor     {result.current_floor:.3f}   the design's unfitted guess"
    )
    lines.append(
        f"  fitted keep_floor      {result.chosen_floor:.3f}   chosen on the calibration split"
    )
    lines.append(
        f"  accuracy (calibration) {result.calibration_accuracy:.0%}   where it was fitted"
    )
    lines.append(
        f"  accuracy (evaluation)  {result.evaluation_accuracy:.0%}   held out — the honest number"
    )

    if result.current_floor <= result.noise_ceiling:
        lines.append(
            f"  ⚠️ the current floor ({result.current_floor}) is at or below the noise "
            f"ceiling ({result.noise_ceiling:.3f}): gibberish can clear it"
        )

    lines.append("\n  top score by question kind:")
    for kind in ("answerable", "out_of_scope", "near_miss", "noise"):
        pool = [s for s in (result.samples + result.noise) if s.kind == kind]
        if pool:
            scores = sorted(s.top_score for s in pool)
            lines.append(
                f"    {kind:<14} min {scores[0]:.3f}  median {scores[len(scores) // 2]:.3f}  "
                f"max {scores[-1]:.3f}"
            )
    return "\n".join(lines)
