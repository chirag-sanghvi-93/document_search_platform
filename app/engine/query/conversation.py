"""One conversational turn: load memory, answer, persist, then summarise.

⚠️ The ordering here is the whole design, and it is deliberate at three points.

**Memory is loaded before answering, summarised after.** The summary is not
needed until the *next* question arrives, so summarising before responding would
put a model call on a critical path that has no use for its result.

**The answer is persisted before the summary is attempted.** A summariser failure
must never cost the turn itself — the record of what was asked and answered is
the durable thing; the summary is derived and can be rebuilt.

**Memory reaches the planner and the synthesizer, and nothing else.** The
verifier and the sufficiency judge never see it. That is not an optimisation:

- the verifier grounds claims against *this turn's* evidence, so admitting
  memory as a source is exactly what makes conversational poisoning possible
- the sufficiency judge asking "is this enough to answer?" would let conversation
  history make weak evidence look adequate

See doc/components/04-conversation-memory.md §5 and §6.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.engine.query import agents, memory
from app.engine.query.pipeline import AnswerResult, ProgressFn, answer_question
from app.shared import tracing
from app.shared.config import Settings
from app.shared.prompts import PromptSet
from app.shared.store import repository
from app.shared.types import ConversationSummary, Turn

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """The answer, plus what memory did around it."""

    answer: AnswerResult
    turn_index: int
    summary: ConversationSummary

    #: Turns that aged out of the verbatim window on this request.
    evicted: int = 0
    #: Counted with the model's own tokenizer, never estimated.
    verbatim_tokens: int = 0
    summary_updated: bool = False
    degradations: list[str] = field(default_factory=list)

    def span_attributes(self) -> dict[str, object]:
        return {
            "turn_index": self.turn_index,
            "memory_verbatim_tokens": self.verbatim_tokens,
            "memory_evicted_turns": self.evicted,
            "memory_summary_updated": self.summary_updated,
        }


async def answer_turn(
    session: Session,
    conversation_id: str,
    question: str,
    *,
    settings: Settings,
    prompts: PromptSet,
    collection: str,
    keep: int | None = None,
    on_progress: ProgressFn | None = None,
) -> TurnResult:
    """Answer one question in the context of a conversation."""
    repository.get_or_create_conversation(session, conversation_id)

    stored = repository.get_conversation_summary(session, conversation_id)
    summary = memory.summary_from_mapping(stored)

    # Read more than the verbatim cap so that eviction has something to evict:
    # the window is chosen by token budget, and the turns beyond it are what the
    # summariser folds in.
    history = repository.get_recent_turns(
        session, conversation_id, limit=settings.conversation.verbatim_turns * 3
    )
    loaded = memory.load(history, summary, settings=settings.conversation, ollama=settings.ollama)

    answer = await answer_question(
        session,
        question,
        settings=settings,
        prompts=prompts,
        collection=collection,
        conversation_summary=loaded.summary_text,
        recent_turns=loaded.recent_turns_text,
        keep=keep,
        on_progress=on_progress,
    )

    next_index = (history[-1].turn_index + 1) if history else 0

    # ---- persist the turn FIRST; the summary is derived and can be rebuilt ----
    repository.write_turn(
        session,
        Turn(
            id="",  # assigned by the database default
            conversation_id=conversation_id,
            turn_index=next_index,
            role="user",
            content=question,
        ),
    )
    repository.write_turn(
        session,
        Turn(
            id="",
            conversation_id=conversation_id,
            turn_index=next_index + 1,
            role="assistant",
            content=answer.answer,
            # ⚠️ Stored so a wrongly-resolved follow-up can be debugged. Which
            # question was actually searched is invisible otherwise.
            rewritten_query=answer.plan.standalone_question,
            # References, not retrieved text. The passage content already lives
            # in the index; duplicating it into every turn would bloat each
            # subsequent memory load with text nothing reads back.
            citations=answer.citations,
            prompt_versions=prompts.versions,
            provenance=answer.provenance,
            latency_ms=answer.latency_ms,
        ),
    )
    session.commit()

    # ---- then summarise, off the critical path --------------------------------
    degradations = list(answer.degradations)
    updated = summary
    if loaded.evicted:
        outcome = await agents.summarize_conversation(
            summary,
            loaded.evicted,
            prompts=prompts,
            settings=settings.ollama,
            conversation=settings.conversation,
        )
        updated = outcome.value
        if outcome.degraded and outcome.reason:
            degradations.append(outcome.reason)
        repository.save_conversation_summary(
            session, conversation_id, memory.summary_to_json(updated)
        )
        session.commit()

    result = TurnResult(
        answer=answer,
        turn_index=next_index + 1,
        summary=updated,
        evicted=len(loaded.evicted),
        verbatim_tokens=loaded.verbatim_tokens,
        summary_updated=bool(loaded.evicted),
        degradations=degradations,
    )
    tracing.set_root_attributes(**result.span_attributes())
    return result
