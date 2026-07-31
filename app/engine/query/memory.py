"""Conversation memory: what the read path is told about earlier turns.

⚠️ The asymmetry this module exists to manage:

> **Documents are treated as evidence. Memory is treated as fact.**

Retrieved passages are scored, filtered, cited and verified. Memory is loaded and
believed — and memory contains the model's own previous output, never re-examined
after the turn that produced it. The same claim therefore gets entirely different
treatment depending on which side it arrives from.

Memory is a record of *what was said*, not *what is true*. Three things follow,
and all three are implemented here rather than asked for in a prompt:

1. The verbatim window is chosen by TOKEN BUDGET, not turn count
2. Evicted turns fold into a STRUCTURED summary with a named slot for what was
   *not* answered, so a hedge cannot be compressed into a finding
3. Assistant turns are rendered WITH their provenance, so a declined answer never
   reads like an established one

What is *not* here: the guard that actually stops poisoning. That one lives in the
pipeline — the verifier is never shown memory, so any claim not traceable to this
turn's evidence is unsupported regardless of where it came from. One rule covers
self-poisoning, laundered uncertainty and false user premises alike, precisely
because it does not care which produced the claim.

See doc/components/04-conversation-memory.md §4-§6.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.shared.config import ConversationSettings, OllamaSettings
from app.shared.tokens import count_tokens
from app.shared.types import ConversationSummary, Turn

logger = logging.getLogger(__name__)

#: Rendered when a turn's answer was not grounded. Kept explicit rather than
#: omitted: an assistant turn shown without qualification reads as established
#: fact to the next turn that sees it.
_PROVENANCE_NOTE = {
    "declined": " [not answered from the documents]",
    "hedged": " [partly unsupported; some claims were retracted]",
    "cited": "",
}


@dataclass(frozen=True)
class LoadedMemory:
    """What one request gets, plus what had to be dropped to fit."""

    summary: ConversationSummary
    verbatim: tuple[Turn, ...]
    evicted: tuple[Turn, ...]

    #: Counted, not estimated. Recorded on the span so a conversation that grew
    #: past its budget is visible rather than merely slower.
    verbatim_tokens: int = 0

    @property
    def summary_text(self) -> str:
        return render_summary(self.summary)

    @property
    def recent_turns_text(self) -> str:
        return render_turns(self.verbatim)


def render_summary(summary: ConversationSummary) -> str:
    """The summary as the planner sees it.

    Empty sections are rendered as "(none)" rather than omitted. An absent
    heading is ambiguous — nothing declined, or the summariser forgot the field?
    — and that ambiguity is exactly what lets `declined` quietly disappear.
    """
    if summary.is_empty:
        return "(none)"

    def section(label: str, items: tuple[str, ...]) -> str:
        return f"{label}: " + ("; ".join(items) if items else "(none)")

    return "\n".join(
        [
            section("Parameters established", summary.parameters),
            section("Topics covered", summary.topics),
            section("Asked but NOT answered", summary.declined),
            section("Open threads", summary.open_threads),
        ]
    )


def render_turns(turns: tuple[Turn, ...]) -> str:
    """Verbatim turns, each assistant turn carrying its provenance.

    ⚠️ The provenance suffix is load-bearing. Turn 3 answering "the allowance is
    30 kg" — wrongly, and while declining — is indistinguishable from a cited
    answer once it is sitting in the context of turn 5. The tag is what keeps a
    refusal legible as a refusal.
    """
    if not turns:
        return "(none)"

    lines: list[str] = []
    for turn in turns:
        note = _PROVENANCE_NOTE.get(turn.provenance or "", "") if turn.role == "assistant" else ""
        lines.append(f"{turn.role}: {turn.content}{note}")
    return "\n".join(lines)


def select_verbatim(
    turns: list[Turn],
    *,
    settings: ConversationSettings,
    ollama: OllamaSettings,
) -> tuple[tuple[Turn, ...], tuple[Turn, ...], int]:
    """Split turns into (kept verbatim, evicted, tokens kept).

    Walks BACKWARDS from the most recent turn, because recency is what reference
    resolution depends on — "what about the other one?" refers to the last thing
    said, not the first.

    ⚠️ A single turn larger than the whole budget is still kept. Dropping it would
    leave the window empty and the pronoun unresolvable, which is worse than
    being over budget; the pipeline's evidence-first ordering absorbs the
    overflow. It is logged, because a conversation that does this repeatedly is
    one where memory is crowding evidence.
    """
    kept: list[Turn] = []
    used = 0

    for turn in reversed(turns):
        if len(kept) >= settings.verbatim_turns:
            break
        cost = count_tokens(f"{turn.role}: {turn.content}", ollama.tokenizer_repo)
        if kept and used + cost > settings.verbatim_budget_tokens:
            break
        if not kept and cost > settings.verbatim_budget_tokens:
            logger.warning(
                "turn %d alone is %d tokens against a %d-token verbatim budget; "
                "keeping it anyway so references stay resolvable",
                turn.turn_index,
                cost,
                settings.verbatim_budget_tokens,
            )
        kept.append(turn)
        used += cost

    kept.reverse()
    kept_ids = {turn.id for turn in kept}
    evicted = tuple(turn for turn in turns if turn.id not in kept_ids)
    return tuple(kept), evicted, used


def load(
    turns: list[Turn],
    summary: ConversationSummary,
    *,
    settings: ConversationSettings,
    ollama: OllamaSettings,
) -> LoadedMemory:
    """Assemble the memory for one request."""
    verbatim, evicted, used = select_verbatim(turns, settings=settings, ollama=ollama)
    return LoadedMemory(summary=summary, verbatim=verbatim, evicted=evicted, verbatim_tokens=used)


# ------------------------------------------------------------------ persistence


def summary_to_json(summary: ConversationSummary) -> str:
    return json.dumps(
        {
            "parameters": list(summary.parameters),
            "topics": list(summary.topics),
            "declined": list(summary.declined),
            "open_threads": list(summary.open_threads),
        }
    )


#: Storage uses the short names; the summariser prompt asks the model for the
#: descriptive ones. Both are accepted on the way in so that a prompt reworded in
#: the registry — where it can be edited without a deploy — cannot silently
#: produce a summary that parses to empty.
_SUMMARY_KEYS = {
    "parameters": ("parameters", "parameters_established"),
    "topics": ("topics", "topics_covered"),
    "declined": ("declined", "declined_unanswered"),
    "open_threads": ("open_threads", "open"),
}


def summary_from_mapping(data: object) -> ConversationSummary:
    """Rebuild a summary from stored JSON or from the summariser's output.

    Tolerant by design: a summary that fails to load must not fail the request.
    The conversation degrades to having no memory, which is a worse answer — not
    a broken one.
    """
    if not isinstance(data, dict):
        return ConversationSummary()

    def field(name: str) -> tuple[str, ...]:
        for key in _SUMMARY_KEYS[name]:
            value = data.get(key)
            if isinstance(value, list):
                return tuple(str(item).strip() for item in value if str(item).strip())
            # A model asked for a list sometimes returns one string anyway.
            if isinstance(value, str) and value.strip():
                return (value.strip(),)
        return ()

    return ConversationSummary(
        parameters=field("parameters"),
        topics=field("topics"),
        declined=field("declined"),
        open_threads=field("open_threads"),
    )


def merge_summaries(
    previous: ConversationSummary, updated: ConversationSummary
) -> ConversationSummary:
    """Union the two, keeping order and dropping duplicates.

    ⚠️ The union is not tidiness — it is the guard against the summariser
    *losing* something. The prompt says to carry forward everything still true,
    but a model that forgets an entry would silently erase a parameter a later
    follow-up depends on, and nothing downstream could tell that had happened.
    Merging in code means a forgetful pass costs nothing.

    One asymmetry: an item the summariser has since recorded as answered is
    removed from `declined`, because that transition is legitimate — the user
    asked again and it was found. The reverse never happens implicitly.
    """

    def union(old: tuple[str, ...], new: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(old + new))

    topics = union(previous.topics, updated.topics)
    answered = {topic.lower() for topic in topics}
    declined = tuple(
        item for item in union(previous.declined, updated.declined) if item.lower() not in answered
    )

    return ConversationSummary(
        parameters=union(previous.parameters, updated.parameters),
        topics=topics,
        declined=declined,
        open_threads=union(previous.open_threads, updated.open_threads),
    )


#: Which field gives way first when the summary will not fit.
#:
#: ⚠️ This order is the design's priorities made executable, and it is the exact
#: inverse of what a naive "drop the oldest" would do.
#:
#: `parameters` is last because it is what a follow-up resolves against — losing
#: "Economy fare" makes the next question quietly answer about the wrong thing.
#: `declined` is second-last because it is the anti-poisoning field: dropping a
#: "could not find" is precisely how a hedge turns into a finding, which is the
#: failure the whole schema exists to prevent.
#: `topics` goes first because it is the most recoverable — it says only what has
#: been discussed, and the verbatim window still holds the recent ones.
_TRIM_ORDER = ("topics", "open_threads", "declined", "parameters")


def trim_summary(
    summary: ConversationSummary, *, max_tokens: int, tokenizer_repo: str
) -> ConversationSummary:
    """Bound the summary, dropping the most recoverable fields first.

    ⚠️ Without this the summary is unbounded. `merge_summaries` unions on every
    eviction and never removes anything, so a long conversation grows it without
    limit — and the failure mode is not a crash. The summary is prepended to the
    planner and synthesizer prompts, so it silently eats the window that evidence
    needs, and the system starts answering from conversation rather than from
    documents. That is exactly the "memory yields, evidence is protected"
    principle inverting itself.

    Within a field the OLDEST entries go first: recent turns are still available
    verbatim, so what the summary uniquely carries is the older context.
    """
    fields = {
        "parameters": list(summary.parameters),
        "topics": list(summary.topics),
        "declined": list(summary.declined),
        "open_threads": list(summary.open_threads),
    }

    def current() -> ConversationSummary:
        return ConversationSummary(
            parameters=tuple(fields["parameters"]),
            topics=tuple(fields["topics"]),
            declined=tuple(fields["declined"]),
            open_threads=tuple(fields["open_threads"]),
        )

    def fits() -> bool:
        return count_tokens(render_summary(current()), tokenizer_repo) <= max_tokens

    while not fits():
        # ⚠️ Fields with MORE THAN ONE entry are drained first, in priority order,
        # before any field is reduced to nothing.
        #
        # A strict priority sweep — empty `topics` completely, then start on
        # `open_threads` — was the first attempt, and a fifty-turn simulation
        # showed why it is wrong: `topics` was annihilated while `open_threads`
        # kept growing. A field reduced to zero entries has stopped doing its
        # job entirely, whereas a field holding only its most recent entry is
        # degraded but still informative. So every field keeps its newest item
        # for as long as the budget allows.
        target = next((name for name in _TRIM_ORDER if len(fields[name]) > 1), None)
        if target is None:
            target = next((name for name in _TRIM_ORDER if fields[name]), None)
        if target is None:
            # Everything emptied and still over budget: only possible when the
            # rendered skeleton alone exceeds it, which means it is misconfigured.
            logger.warning(
                "summary could not be trimmed to %d tokens even when emptied; "
                "check conversation.summary_max_tokens",
                max_tokens,
            )
            return ConversationSummary()
        # Oldest first: recent turns are still available verbatim, so between two
        # summary entries the newer is the one more likely to still matter.
        fields[target].pop(0)

    trimmed = current()
    if trimmed != summary:
        logger.info("summary trimmed to fit %d tokens", max_tokens)
    return trimmed
