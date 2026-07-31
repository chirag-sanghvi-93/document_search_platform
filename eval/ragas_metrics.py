"""RAGAs metrics, judged by a LOCAL model.

⚠️ Two things about this module are deliberate and easy to get wrong.

**The judge is a different model family from the answering model.** `gemma3:12b`
grades what `qwen2.5:7b` produced. A model grading its own output agrees with
itself — the failure is not that scores are wrong but that they are
uninformative, and uninformative scores are worse than none because they look
like evidence.

**Nothing here reaches a hosted API.** RAGAs defaults to OpenAI, so leaving the
LLM and embeddings unset would make an evaluation that claims to be
self-contained depend silently on someone's API key and send the corpus to a
third party. Both are bound explicitly to the local Ollama host.

RAGAs measures answers that were *given*: faithfulness, relevancy, and whether
retrieval supplied what was needed. It says nothing about whether the system was
right to answer at all — that half lives in `eval/harness.py`, and the two are
reported together precisely so neither is mistaken for the whole.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.engine.query.pipeline import answer_question
from app.shared.config import Settings
from app.shared.prompts import PromptSet
from eval.datasets import ANSWERABLE

logger = logging.getLogger(__name__)


@dataclass
class RagasReport:
    scores: dict[str, float] = field(default_factory=dict)
    samples: int = 0
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None and bool(self.scores)


def _build_judge(settings: Settings) -> tuple[Any, Any]:
    """Bind RAGAs to the local Ollama host — never to a hosted default."""
    from langchain_ollama import ChatOllama, OllamaEmbeddings
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper

    judge = ChatOllama(
        model=settings.ollama.judge_model,
        base_url=settings.ollama.host,
        temperature=0.0,
    )
    embeddings = OllamaEmbeddings(
        model=settings.ollama.embedding_model, base_url=settings.ollama.host
    )
    return LangchainLLMWrapper(judge), LangchainEmbeddingsWrapper(embeddings)


async def collect_samples(
    session: Session, *, settings: Settings, prompts: PromptSet, collection: str
) -> list[dict[str, Any]]:
    """Run the answerable set and capture what RAGAs needs to judge.

    ⚠️ ONLY the answerable questions. Scoring faithfulness on a question the
    system correctly refused is meaningless — there is no answer to be faithful
    with, and including them would drag every average down for behaving
    correctly. Refusals are measured in the custom harness, where declining is
    the thing being scored rather than a missing answer.
    """
    samples: list[dict[str, Any]] = []
    for item in ANSWERABLE:
        result = await answer_question(
            session,
            item.question,
            settings=settings,
            prompts=prompts,
            collection=collection,
        )
        if result.provenance == "declined" or not result.passages:
            logger.info("skipping %r for RAGAs: declined", item.question)
            continue
        samples.append(
            {
                "user_input": item.question,
                "response": result.answer,
                # display_text, exactly what the synthesizer saw — never the
                # embedding text, which carries a model-written preamble.
                "retrieved_contexts": [p.display_text for p in result.passages],
                # No hand-written ground truth: context_recall is judged against
                # the answer instead. Inventing a reference answer would measure
                # agreement with whoever wrote it.
                "reference": result.answer,
            }
        )
    return samples


def score(samples: list[dict[str, Any]], settings: Settings) -> RagasReport:
    if not samples:
        return RagasReport(error="no answerable samples produced an answer")

    try:
        from datasets import Dataset
        from ragas import evaluate
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
            context_recall,
            faithfulness,
        )

        judge, embeddings = _build_judge(settings)
        result = evaluate(
            Dataset.from_list(samples),
            metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
            llm=judge,
            embeddings=embeddings,
            raise_exceptions=False,
        )

        scores: dict[str, float] = {}
        raw: dict[str, Any] = dict(result)  # type: ignore[call-overload]
        for name, value in raw.items():
            try:
                scores[name] = round(float(value), 3)
            except (TypeError, ValueError):
                continue
        return RagasReport(scores=scores, samples=len(samples))

    except Exception as exc:
        # ⚠️ Reported, never raised. RAGAs is one half of the evaluation; a
        # dependency problem in it must not discard the half that already ran.
        logger.warning("RAGAs failed (%s)", exc)
        return RagasReport(error=f"{type(exc).__name__}: {exc}", samples=len(samples))


def render(report: RagasReport) -> str:
    lines = ["\n## RAGAs  (judged by a different model family)"]
    if not report.available:
        lines.append(f"  unavailable — {report.error}")
        return "\n".join(lines)

    meaning = {
        "faithfulness": "claims supported by the retrieved passages",
        "answer_relevancy": "the answer addresses the question asked",
        "context_precision": "retrieved passages were actually useful",
        "context_recall": "retrieval found what the answer needed",
    }
    lines.append(f"  samples: {report.samples}  (answerable only — refusals are not scored here)")
    for name, value in report.scores.items():
        lines.append(f"  {name:<22} {value:>6.3f}   {meaning.get(name, '')}")
    return "\n".join(lines)
