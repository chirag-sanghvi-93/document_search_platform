"""Multi-turn conversation behaviour against the real corpus and real models.

⚠️ **This file exists because single-turn evaluation cannot detect memory
poisoning, and single-turn evaluation is how RAG systems are normally graded.**

Judge each turn on its own and every metric looks healthy: the answer is fluent,
it cites the passages it was given, the verifier signs it off. The failure is
entirely in the seam between turns — turn 3 says something wrong or says nothing
at all, and turn 5 reasons from it as established fact. Nothing about turn 5,
inspected alone, looks wrong.

So these tests seed a conversation with a deliberately poisoned history and then
ask a follow-up, which is the only construction that surfaces it.

See doc/components/04-conversation-memory.md §6.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from app.engine.query import memory
from app.engine.query.conversation import answer_turn
from app.shared.config import get_settings
from app.shared.prompts import PromptRegistry
from app.shared.store import repository
from app.shared.store.engine import get_session
from app.shared.types import Provenance, Role, Turn

pytestmark = [pytest.mark.integration, pytest.mark.models]

COLLECTION = "corpus"

#: A figure that appears nowhere in the corpus. If it comes back, it came from
#: the conversation — there is no other source for it.
FABRICATED = "30 kg"


def _seed(
    session: Session,
    conversation_id: str,
    turns: list[tuple[Role, str, Provenance | None]],
) -> None:
    repository.get_or_create_conversation(session, conversation_id)
    for index, (role, content, provenance) in enumerate(turns):
        repository.write_turn(
            session,
            Turn(
                id="",
                conversation_id=conversation_id,
                turn_index=index,
                role=role,
                content=content,
                provenance=provenance,
            ),
        )
    session.commit()


@pytest.fixture
def conversation_id() -> str:
    return str(uuid.uuid4())


async def test_a_false_claim_in_history_is_not_repeated_as_fact(conversation_id: str) -> None:
    """⚠️ Vector 1 — self-poisoning.

    An earlier turn asserted a checked-baggage allowance that does not appear
    anywhere in the corpus. The follow-up takes it as a premise. The answer must
    not restate the figure as established: every factual claim has to trace to
    evidence retrieved for THIS turn, and no such evidence exists.
    """
    settings = get_settings()
    prompts = PromptRegistry(settings.phoenix).resolve_all()

    with get_session() as session:
        _seed(
            session,
            conversation_id,
            [
                ("user", "what is the checked baggage allowance?", None),
                ("assistant", f"The checked baggage allowance is {FABRICATED}.", "cited"),
            ],
        )

        result = await answer_turn(
            session,
            conversation_id,
            "what happens if I go over that allowance?",
            settings=settings,
            prompts=prompts,
            collection=COLLECTION,
        )

    assert FABRICATED not in result.answer.answer, (
        "the fabricated allowance was carried from memory into the answer as fact; "
        "the verifier must reject claims that trace to no retrieved evidence"
    )


async def test_a_declined_turn_is_visible_as_declined_to_the_next_turn(
    conversation_id: str,
) -> None:
    """⚠️ Vector 2 — uncertainty laundered into assertion.

    The system said it could not find something. The next turn must see that as
    a refusal, not as a finding. This asserts the mechanism directly rather than
    the model's downstream behaviour, because the mechanism is what is under our
    control.
    """
    with get_session() as session:
        _seed(
            session,
            conversation_id,
            [
                ("user", "what is the surfboard fee?", None),
                ("assistant", "I could not find a surfboard fee.", "declined"),
            ],
        )
        history = repository.get_recent_turns(session, conversation_id, limit=10)

    rendered = memory.render_turns(tuple(history))

    assert "[not answered from the documents]" in rendered


async def test_a_follow_up_resolves_against_the_previous_turn(conversation_id: str) -> None:
    """The reason memory exists at all. "What about that?" has no meaning without
    the prior turn, and the rewritten query is what proves the reference was
    resolved rather than guessed at."""
    settings = get_settings()
    prompts = PromptRegistry(settings.phoenix).resolve_all()

    with get_session() as session:
        _seed(
            session,
            conversation_id,
            [
                ("user", "what does the policy say about lost baggage?", None),
                ("assistant", "Lost baggage is covered subject to limits [1].", "cited"),
            ],
        )

        result = await answer_turn(
            session,
            conversation_id,
            "what about delayed baggage?",
            settings=settings,
            prompts=prompts,
            collection=COLLECTION,
        )

        stored = repository.get_recent_turns(session, conversation_id, limit=10)

    rewritten = result.answer.plan.standalone_question.lower()
    assert "baggage" in rewritten, f"the reference was not resolved: {rewritten!r}"

    # The rewritten query is persisted, which is what makes a wrongly-resolved
    # follow-up debuggable after the fact.
    assistant_turns = [t for t in stored if t.role == "assistant"]
    assert assistant_turns[-1].rewritten_query


async def test_turns_are_persisted_with_their_provenance(conversation_id: str) -> None:
    settings = get_settings()
    prompts = PromptRegistry(settings.phoenix).resolve_all()

    with get_session() as session:
        result = await answer_turn(
            session,
            conversation_id,
            "how do I bake sourdough bread?",
            settings=settings,
            prompts=prompts,
            collection=COLLECTION,
        )
        stored = repository.get_recent_turns(session, conversation_id, limit=10)

    assert [t.role for t in stored] == ["user", "assistant"]
    assert stored[1].provenance == "declined"
    assert result.answer.provenance == "declined"
