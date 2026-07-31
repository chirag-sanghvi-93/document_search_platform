"""OpenAI-compatible chat surface.

⚠️ The contract is adopted, not designed. Exposing `/v1/chat/completions` and
`/v1/models` means no plugin code exists anywhere, streaming semantics come from
a specification rather than from us, and — the part that matters most in
practice — **the backend is not locked to one frontend**. Any compatible client
drives it, including `curl`, so the integration is testable without the interface
running at all.

The standard schema has no slot for three things this system needs, and each is
solved without adding a non-standard field that would recreate the coupling:

  citations   -> markdown in the message content, collapsed with <details>
  progress    -> reasoning content, which clients render collapsed
  identity    -> a header, falling back to a hash of the opening message

See doc/components/10-openwebui.md — the authority.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import anyio
from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.engine.query.conversation import TurnResult, answer_turn
from app.shared.config import Settings, get_settings
from app.shared.store.engine import get_session
from app.shared.types import Citation

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


@dataclass(frozen=True)
class Profile:
    """A named retrieval profile, exposed to the client as a model.

    ⚠️ The profiles differ by how many passages are kept, NOT by model.
    The design originally paired them with different models — a smaller one for
    speed — but every reasoning model was measured off this path (see
    `verifier_model` in app/shared/config.py), and the remaining non-reasoning
    candidate failed to catch a fabricated figure during verification. Trading
    hallucination detection for latency is not a profile, so the model axis was
    dropped and the honest one kept.
    """

    id: str
    keep: int
    description: str


PROFILES: dict[str, Profile] = {
    "agentic-rag": Profile("agentic-rag", keep=5, description="Quality — 5 passages"),
    "agentic-rag-fast": Profile("agentic-rag-fast", keep=3, description="Speed — 3 passages"),
}
DEFAULT_PROFILE = "agentic-rag"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = ""


class ChatRequest(BaseModel):
    model: str = DEFAULT_PROFILE
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False


# ------------------------------------------------------------------- identity


def conversation_id_for(request_id: str | None, messages: list[ChatMessage]) -> str:
    """Resolve the conversation this request belongs to.

    The standard contract is stateless — every request carries the whole message
    array and no identifier. Server-side memory (rolling summaries, provenance
    tags, the poisoning guards) cannot be reconstructed from a raw array, so it
    needs a key.

    An explicit `X-Conversation-Id` wins. Otherwise the id is a hash of the first
    user message, which is stable because that message does not change as the
    conversation grows.

    ⚠️ Two users opening with an identical first question would collide. A user
    identifier resolves it where one exists; for a single-user deployment bound
    to localhost the risk is accepted, and recorded here rather than left to be
    discovered. See doc/components/10-openwebui.md §6.
    """
    if request_id:
        return request_id

    first_user = next((m.content for m in messages if m.role == "user"), "")
    digest = hashlib.sha256(first_user.encode()).hexdigest()
    # Formatted as a UUID because the conversations table keys on one.
    return str(uuid.UUID(digest[:32]))


def latest_question(messages: list[ChatMessage]) -> str:
    """The message actually being answered.

    Earlier turns are deliberately ignored: the client resends the full history
    every time, but this system's memory is server-side and already holds a
    curated version of it. Feeding the client's copy in as well would put
    unfiltered assistant output back into context — the very thing the memory
    layer strips provenance-tags onto and bounds.
    """
    for message in reversed(messages):
        if message.role == "user":
            return message.content
    return ""


# ------------------------------------------------------------------ rendering


def render_citations(citations: tuple[Citation, ...]) -> str:
    """Sources as plain markdown — a heading and a blockquote per source.

    ⚠️ NOT `<details>`, and this was verified rather than assumed.

    The design preferred collapsed HTML and flagged it as conditional on the
    renderer permitting raw HTML, with a plain list as the documented fallback.
    Against the pinned image the answer is **no**: OpenWebUI v0.6.5 renders
    `<details><summary>` as literal text, so every answer ended with visible
    markup and the quotes never collapsed. The fallback is verbose but cannot
    break, which is the trade the design already chose in advance.

    The schema still has no citation field, so this stays inside the message
    content: depending on a non-standard field would recreate exactly the
    frontend coupling the OpenAI-compatible choice exists to avoid.
    """
    if not citations:
        return ""
    lines = ["\n\n---\n**Sources**\n"]
    for citation in citations:
        location = f"{citation.title} · p.{citation.page}"
        if citation.section:
            location += f" · {citation.section}"
        lines.append(f"**[{citation.number}]** {location}")
        lines.append(f"> {citation.quote}\n")
    return "\n".join(lines)


def render_near_misses(near: tuple[Citation, ...]) -> str:
    """⚠️ Labelled so it cannot be mistaken for a source list.

    These are what was found when nothing answered the question. The risk is a
    reader skimming the heading and treating them as support for an answer that
    was never given, so the wording is explicit and they carry no bracketed
    numbers.
    """
    if not near:
        return ""
    lines = ["\n\n---\n*Closest matches — these do **not** answer the question:*\n"]
    for citation in near:
        location = f"{citation.title} · p.{citation.page}"
        if citation.section:
            location += f" · {citation.section}"
        lines.append(f"- {location}")
    return "\n".join(lines)


def compose(result: TurnResult) -> str:
    """The full message content: answer, then sources or closest matches."""
    answer = result.answer
    if answer.provenance == "declined":
        return answer.answer + render_near_misses(answer.near_misses)
    return answer.answer + render_citations(answer.citations)


# --------------------------------------------------------------------- SSE


def _chunk(completion_id: str, model: str, delta: dict[str, Any]) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _final(completion_id: str, model: str) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n"


async def _stream(
    question: str,
    conversation_id: str,
    profile: Profile,
    settings: Settings,
    prompts: Any,
    collection: str,
) -> AsyncIterator[str]:
    """Progress as reasoning, then the verified answer.

    ⚠️ The DRAFT is never streamed. Verification runs after drafting and may
    retract claims, so streaming the draft would show a claim appearing and then
    vanishing — for a policy-lookup tool that is worse than a pause. Only text
    that survived verification is sent.

    Progress fills the wait instead. Streamed deltas are append-only, so
    "Searching…" cannot later be replaced — but reasoning content is rendered
    collapsed and folds away once the answer arrives, which sidesteps the problem
    rather than fighting it.
    """
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    yield _chunk(completion_id, profile.id, {"role": "assistant", "content": ""})

    send, receive = anyio.create_memory_object_stream[str](max_buffer_size=64)

    def on_progress(message: str) -> None:
        # Never block the engine to report progress: if nothing is draining the
        # stream, the update is dropped rather than stalling the answer.
        with contextlib.suppress(anyio.WouldBlock):
            send.send_nowait(message)

    result: TurnResult | None = None
    failure: Exception | None = None

    async def run() -> None:
        nonlocal result, failure
        try:
            with get_session() as session:
                result = await answer_turn(
                    session,
                    conversation_id,
                    question,
                    settings=settings,
                    prompts=prompts,
                    collection=collection,
                    keep=profile.keep,
                    on_progress=on_progress,
                )
        except Exception as exc:  # engine failure — reported, never a broken stream
            logger.exception("chat request failed")
            failure = exc
        finally:
            send.close()

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        async for message in receive:
            yield _chunk(completion_id, profile.id, {"reasoning_content": f"{message}\n"})

    if failure is not None or result is None:
        # ⚠️ A well-formed stream carrying an error message, not a truncated one.
        # A client that sees the connection drop mid-stream shows a spinner
        # forever; one that receives text can say what went wrong.
        yield _chunk(
            completion_id,
            profile.id,
            {"content": "Something went wrong answering that. Please try again."},
        )
        yield _final(completion_id, profile.id)
        return

    yield _chunk(completion_id, profile.id, {"content": compose(result)})
    yield _final(completion_id, profile.id)


# ------------------------------------------------------------------- routes


@router.get("/v1/models")
async def list_models() -> dict[str, Any]:
    """The profiles, as models. The client needs something to offer in its picker."""
    return {
        "object": "list",
        "data": [
            {
                "id": profile.id,
                "object": "model",
                "created": 0,
                "owned_by": "document-search-platform",
                "description": profile.description,
            }
            for profile in PROFILES.values()
        ],
    }


@router.post("/v1/chat/completions")
async def chat_completions(
    body: ChatRequest,
    request: Request,
    x_conversation_id: str | None = Header(default=None),
) -> Any:
    settings = get_settings()
    profile = PROFILES.get(body.model, PROFILES[DEFAULT_PROFILE])
    prompts = request.app.state.prompts.resolve_all()
    collection = settings.retrieval.collection

    question = latest_question(body.messages)
    conversation_id = conversation_id_for(x_conversation_id, body.messages)

    if body.stream:
        return StreamingResponse(
            _stream(question, conversation_id, profile, settings, prompts, collection),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    with get_session() as session:
        result = await answer_turn(
            session,
            conversation_id,
            question,
            settings=settings,
            prompts=prompts,
            collection=collection,
            keep=profile.keep,
        )

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": profile.id,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": compose(result)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
