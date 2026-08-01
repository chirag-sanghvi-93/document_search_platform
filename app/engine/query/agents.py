"""The four agents: planner, retrieval specialist, synthesizer, verifier.

Each is a single model call with a **structured** output. Parsing is strict and
failures are named — a half-parsed structure propagating downstream is worse than
an error, because it produces a plausible answer built on a misread plan.

Every agent degrades rather than raising, and every degradation is reported so it
can be recorded on the trace. `doc/02-architecture.md` §7 is explicit that
fail-forward makes failures invisible; the `AgentOutcome.degraded` flag is how
this module refuses to be silent about it.

Model assignment:
  planner, retrieval specialist, synthesizer  ->  answering_model (qwen2.5:7b)
  verifier                                    ->  verifier_model  (qwen2.5:7b)

A fifth agent, the conversation summariser, lives here too but is not part of
answering a question: it runs *after* the response has been returned, because the
summary it produces is not needed until the next one arrives.

⚠️ None of these is a reasoning model, and every attempt to make one so was
reversed on measurement. The read path originally ran qwen3:8b for the first
three and qwen3:4b for the verifier, on the reasonable-sounding argument that
planning and grounding are exactly what reasoning helps with, at only one call
each per question. Measured, that configuration answered one question in 553
seconds; the verifier alone accounted for 177 of the remaining 233. Quality did
not pay for it — the non-reasoning verifier retracted a fabricated figure with
byte-identical output. See app/shared/config.py for the numbers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

import httpx

from app.shared.config import ConversationSettings, OllamaSettings
from app.shared.prompts import PromptSet
from app.shared.types import ConversationSummary, Intent, Passage, Plan, Turn

logger = logging.getLogger(__name__)


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_VALID_INTENTS: tuple[Intent, ...] = (
    "lookup",
    "comparison",
    "summary",
    "capability",
    "out_of_scope",
)


class AgentError(RuntimeError):
    """Raised when a model response cannot be parsed into the expected shape.

    Deliberately named rather than allowing a KeyError or TypeError to surface
    from deep inside parsing — the caller needs to know *which* agent failed to
    decide how to degrade.
    """


@dataclass
class AgentOutcome[T]:
    """An agent's result, plus whether it had to fall back.

    `degraded` is not decoration. A system whose planner falls back on every
    single request looks entirely healthy from its answers alone.
    """

    value: T
    degraded: bool = False
    reason: str | None = None
    model_calls: int = 0


@dataclass
class Sufficiency:
    sufficient: bool
    reasoning: str = ""
    new_query: str | None = None


@dataclass
class Verification:
    verified_answer: str
    claims_retracted: int = 0
    claims_hedged: int = 0
    unverified: bool = False


@dataclass
class CallLog:
    """Running count of model calls for one question, for the root span."""

    calls: int = 0
    by_agent: dict[str, int] = field(default_factory=dict)

    def record(self, agent: str, n: int = 1) -> None:
        self.calls += n
        self.by_agent[agent] = self.by_agent.get(agent, 0) + n


async def _generate(
    prompt_text: str, model: str, settings: OllamaSettings, *, num_predict: int | None = None
) -> str:
    options: dict[str, Any] = {
        "temperature": settings.temperature,
        "num_ctx": settings.num_ctx,
    }
    if num_predict is not None:
        options["num_predict"] = num_predict

    async with httpx.AsyncClient(
        base_url=settings.host, timeout=settings.request_timeout_s
    ) as client:
        response = await client.post(
            "/api/generate",
            json={"model": model, "prompt": prompt_text, "stream": False, "options": options},
        )
        response.raise_for_status()
        return str(response.json()["response"])


def _extract_json(raw: str) -> dict[str, Any]:
    """Pull a JSON object out of a model response.

    Reasoning models wrap their answer in prose even when told not to, so the
    first `{...}` block is taken rather than assuming the whole response parses.
    """
    match = _JSON_BLOCK.search(raw)
    if match is None:
        raise AgentError(f"no JSON object in response: {raw[:200]!r}")
    try:
        # ⚠️ strict=False permits literal newlines and tabs inside string values.
        # Models emit them constantly — a verified answer of more than one
        # sentence usually contains one — and strict parsing rejected the whole
        # object over it. That sent perfectly good answers down the degradation
        # path to be returned marked "could not be verified", which is worse than
        # the formatting it was objecting to.
        parsed = json.loads(match.group(0), strict=False)
    except json.JSONDecodeError as exc:
        raise AgentError(f"malformed JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AgentError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


# --------------------------------------------------------------------- planner

#: A capitalised word sequence — "Delta Air Lines", "Etihad Airways". Used to
#: pick organisation and document identifiers out of the corpus description.
_PROPER_NOUN = re.compile(r"\b[A-Z][a-z]{2,}(?:\s+(?:of\s+|the\s+)?[A-Z][a-z]{2,})*\b")

#: Grammatical words only — English, not domain vocabulary. Anything that varies
#: by corpus is handled structurally in `_corpus_identifiers`, never listed here.
_SENTENCE_OPENERS = frozenset(
    {"this", "the", "these", "those", "it", "its", "they", "their", "there", "we", "our"}
)

#: A proper noun appearing in more than this share of documents is domain
#: vocabulary, not a document identifier. "Conditions" in an airline corpus,
#: "Patients" in a medical one — the WORDS differ, the rule does not.
_UBIQUITY_THRESHOLD = 0.5


def _proper_nouns(text: str) -> set[str]:
    found: set[str] = set()
    for match in _PROPER_NOUN.finditer(text):
        phrase = match.group(0).strip()
        if phrase.lower() in _SENTENCE_OPENERS:
            continue
        found.add(phrase.lower())
        for word in phrase.split():
            if len(word) > 3 and word.lower() not in _SENTENCE_OPENERS:
                found.add(word.lower())
    return found


def _corpus_identifiers(corpus_description: str) -> frozenset[str]:
    """Names that DISTINGUISH one document from another.

    ⚠️ Derived structurally, never from a list of domain words.

    An earlier version excluded "passengers", "conditions", "coverage" and
    similar by name. That worked on an airline corpus and would have failed
    silently on any other: on a medical corpus "Patients" and "Diagnosis" would
    be treated as document identifiers, and every sub-question mentioning them
    would be stripped of its subject. Corpus vocabulary had leaked into code that
    is supposed to work on an unknown corpus.

    The rule instead: the description carries one line per document, so a proper
    noun that appears in MOST documents is shared vocabulary, while one appearing
    in few is a name that tells documents apart. "Etihad Airways" appears in
    some; "Conditions" appears in nearly all. The words are corpus-specific; the
    test is not.
    """
    lines = [line for line in corpus_description.splitlines() if line.strip()]
    # One line means nothing to compare against — fall back to plain extraction.
    if len(lines) < 2:
        return frozenset(_proper_nouns(corpus_description))

    per_line = [_proper_nouns(line) for line in lines]
    counts: dict[str, int] = {}
    for nouns in per_line:
        for noun in nouns:
            counts[noun] = counts.get(noun, 0) + 1

    limit = max(1, int(len(lines) * _UBIQUITY_THRESHOLD))
    return frozenset(noun for noun, count in counts.items() if count <= limit)


#: Prepositions that introduce the "…in Delta Air Lines' conditions of carriage"
#: clause. Cutting from one of these removes the narrowing and leaves the rest of
#: the question — including any pronoun the planner correctly resolved.
_SCOPE_CONNECTIVE = re.compile(
    r"\b(?:according to|as set out in|as stated in|set out in|specified in|stated in|"
    r"described in|outlined in|listed in|under|within|per|from|in|on|of|for|by)\b",
    re.IGNORECASE,
)

#: Below this many words, a stripped question has lost its subject and is a worse
#: search query than the user's own words.
_MIN_STRIPPED_WORDS = 4


def _strip_invented_clause(sub_question: str, identifier: str) -> str | None:
    """Remove the clause that names `identifier`, keeping the rest of the question.

    Returns None when nothing usable is left — the caller then falls back to the
    user's own question.
    """
    lowered = sub_question.lower()
    start = lowered.find(identifier)
    if start == -1:
        return None

    head = sub_question[:start]
    tail_match = re.search(r"[,.?!;]", sub_question[start:])
    tail = sub_question[start + tail_match.start() :] if tail_match else ""

    # ⚠️ The LAST connective before the document name, not the first.
    # "What are the rules on excess baggage in Delta Air Lines' conditions..."
    # contains two ("on", "in"); cutting at the first leaves "What are the
    # rules?" and throws away the subject the question was actually about.
    connectives = list(_SCOPE_CONNECTIVE.finditer(head))
    if connectives:
        head = head[: connectives[-1].start()]
    head = head.strip().rstrip(",;")
    if len(head.split()) < _MIN_STRIPPED_WORDS:
        return None

    rebuilt = f"{head}{tail}".strip()
    if not rebuilt.endswith(("?", ".")):
        rebuilt += "?"
    return rebuilt


def _reject_invented_scope(
    subs: tuple[str, ...], question: str, context: str, corpus_description: str
) -> tuple[str, ...]:
    """Remove narrowing to a document the user never named, keeping the rest.

    ⚠️ Strips the offending clause rather than discarding the whole sub-question,
    and that distinction is not cosmetic — the wholesale version was a real bug.

    Asked "what happens if I go over that allowance?" after an earlier turn about
    checked baggage, the planner returned:

        "What happens if I exceed the checked baggage allowance
         on Delta Air Lines' domestic conditions of carriage?"

    which is one good thing and one bad thing in a single string: the pronoun
    "that allowance" was correctly resolved, AND a document nobody named was
    bolted on. Replacing it with the user's raw question threw away the
    resolution too, so the search ran on the literal words "that allowance" and
    the follow-up declined. Harmless before conversation memory existed, because
    without a conversation the raw question has no pronouns to lose.
    This is a code guard because prompting did not hold. The planner prompt
    states the rule explicitly ("Do NOT add qualifiers the user did not state.
    If they did not name a specific document, neither should your
    sub-questions") and the model violated it on every trial, across two
    phrasings of the surrounding corpus block.

    The narrowing itself is not cosmetic either. A sub-question IS the search
    query, so "what is the excess baggage charge?" silently becoming "...in Delta
    Air Lines' domestic conditions of carriage?" pulls the embedding toward one
    document and drops the clause in another that actually answers it. The
    observed result was five Delta passages, a relevant Etihad clause excluded,
    and an answer that declined — on a corpus that contained the answer.

    Names the user or the conversation introduced are kept; only names the model
    took from the corpus description unprompted are removed.

    Known limitation: this catches organisation and document identifiers, not
    lowercase qualifiers such as "domestic". Those narrow less sharply because
    they do not name a document, but they are not caught here.
    """
    identifiers = _corpus_identifiers(corpus_description)
    if not identifiers:
        return subs

    permitted = f"{question}\n{context}".lower()
    allowed = {term for term in identifiers if term in permitted}

    cleaned: list[str] = []
    for sub in subs:
        lowered = sub.lower()
        invented = sorted(t for t in identifiers - allowed if t in lowered)
        if not invented:
            cleaned.append(sub)
            continue

        # Longest match first, so "Delta Air Lines" goes before "delta" and the
        # leftover word is not stranded mid-sentence. An identifier already
        # removed by an earlier pass is simply skipped — its absence means the
        # strip worked, not that it failed.
        stripped: str | None = sub
        for identifier in sorted(invented, key=len, reverse=True):
            if stripped is None:
                break
            if identifier not in stripped.lower():
                continue
            stripped = _strip_invented_clause(stripped, identifier)

        replacement = stripped if stripped else question
        logger.info(
            "planner narrowed to %s, which the user did not name; searching %r instead",
            ", ".join(invented),
            replacement,
        )
        cleaned.append(replacement)

    # Two narrowed sub-questions both collapse to the question; search it once.
    return tuple(dict.fromkeys(cleaned))


def _parse_plan(raw: str, question: str, cap: int) -> Plan:
    data = _extract_json(raw)

    intent_raw = str(data.get("intent", "")).strip().lower()
    if intent_raw not in _VALID_INTENTS:
        raise AgentError(f"invalid intent {intent_raw!r}")
    intent: Intent = intent_raw

    standalone = str(data.get("standalone_question") or question).strip() or question

    raw_subs = data.get("sub_questions") or []
    if not isinstance(raw_subs, list):
        raise AgentError("sub_questions must be a list")
    subs = tuple(str(s).strip() for s in raw_subs if str(s).strip())[:cap]

    if intent not in ("out_of_scope", "capability") and not subs:
        # The model classified as answerable but gave nothing to search for.
        # Falling back to the question itself is strictly better than failing.
        subs = (standalone,)

    return Plan(intent=intent, standalone_question=standalone, sub_questions=subs)


async def plan(
    question: str,
    *,
    corpus_description: str,
    conversation_summary: str,
    recent_turns: str,
    prompts: PromptSet,
    settings: OllamaSettings,
    sub_question_cap: int,
    call_log: CallLog | None = None,
) -> AgentOutcome[Plan]:
    """Classify, rewrite and decompose — in ONE call, not three.

    Degrades to a single sub-question with no rewriting, which is exactly the
    behaviour of an ordinary non-agentic retrieval pipeline. That is the right
    fallback: the system gets worse, not broken.
    """
    prompt = prompts["planner"]
    rendered = prompt.render(
        corpus_description=corpus_description,
        conversation_summary=conversation_summary,
        recent_turns=recent_turns,
        question=question,
    )

    try:
        raw = await _generate(rendered, settings.answering_model, settings)
        if call_log:
            call_log.record("planner")
        parsed = _parse_plan(raw, question, sub_question_cap)

        # ⚠️ BOTH fields, not just the sub-questions. `standalone_question` is what
        # the synthesizer is asked to answer, so cleaning only the search queries
        # fixed retrieval and left the answer still reading "...for Delta Air
        # Lines' domestic conditions of carriage" — now over passages that were
        # no longer Delta-only. Retrieval and synthesis have to be narrowed, or
        # not narrowed, together.
        context = f"{conversation_summary}\n{recent_turns}"
        parsed = replace(
            parsed,
            sub_questions=_reject_invented_scope(
                parsed.sub_questions, question, context, corpus_description
            ),
            standalone_question=_reject_invented_scope(
                (parsed.standalone_question,), question, context, corpus_description
            )[0],
        )
        return AgentOutcome(value=parsed, model_calls=1)
    except (AgentError, httpx.HTTPError) as exc:
        logger.warning("planner failed (%s); degrading to single-question lookup", exc)
        return AgentOutcome(
            value=Plan(intent="lookup", standalone_question=question, sub_questions=(question,)),
            degraded=True,
            reason=f"planner: {type(exc).__name__}",
            model_calls=1,
        )


# ----------------------------------------------------------- retrieval specialist


def _format_passages(passages: list[Passage], *, numbered: bool) -> str:
    if not passages:
        return "(no passages retrieved)"
    from app.engine.query.citations import sanitize_for_prompt

    lines: list[str] = []
    for i, p in enumerate(passages, 1):
        prefix = f"[{i}] " if numbered else "- "
        # display_text ONLY. embedding_text carries a model-written preamble, and
        # quoting that under a page citation would attribute machine-generated
        # words to the source document.
        #
        # ⚠️ Sanitized so that the passage's OWN bracketed numerals — clause
        # references, enumerated sub-paragraphs, which airline and insurance
        # conditions are full of — cannot be mistaken for citation markers when
        # the model echoes the wording back. Every bracket the model sees is one
        # we introduced. The reader still sees the original text.
        lines.append(f"{prefix}{sanitize_for_prompt(p.display_text)}")
    return "\n\n".join(lines)


async def judge_sufficiency(
    sub_question: str,
    passages: list[Passage],
    *,
    attempt: int,
    max_attempts: int,
    prompts: PromptSet,
    settings: OllamaSettings,
    call_log: CallLog | None = None,
) -> AgentOutcome[Sufficiency]:
    """Decide whether the passages answer the sub-question, and if not, how to
    search differently.

    Only called when the score is genuinely ambiguous — the caller short-circuits
    at both extremes, which is what keeps a simple lookup at four model calls.
    """
    prompt = prompts["retrieval-specialist"]
    rendered = prompt.render(
        sub_question=sub_question,
        passages=_format_passages(passages, numbered=False),
        attempt=attempt,
        max_attempts=max_attempts,
    )

    try:
        raw = await _generate(rendered, settings.answering_model, settings)
        if call_log:
            call_log.record("retrieval-specialist")
        data = _extract_json(raw)
        new_query = data.get("new_query")
        return AgentOutcome(
            value=Sufficiency(
                sufficient=bool(data.get("sufficient", True)),
                reasoning=str(data.get("reasoning", "")),
                new_query=str(new_query).strip() if new_query else None,
            ),
            model_calls=1,
        )
    except (AgentError, httpx.HTTPError) as exc:
        logger.warning("retrieval specialist failed (%s); accepting current passages", exc)
        # Degrade to "sufficient": one plain search, no retry. Retrying on a
        # failed judgement risks burning the shared budget on guesses.
        return AgentOutcome(
            value=Sufficiency(sufficient=True, reasoning="judgement unavailable"),
            degraded=True,
            reason=f"retrieval-specialist: {type(exc).__name__}",
            model_calls=1,
        )


# ----------------------------------------------------------------- synthesizer


async def synthesize(
    question: str,
    passages: list[Passage],
    *,
    conversation_summary: str,
    prompts: PromptSet,
    settings: OllamaSettings,
    call_log: CallLog | None = None,
) -> AgentOutcome[str]:
    """Draft the answer from numbered passages, marking claims with [n].

    ⚠️ Sees `display_text` only — enforced in `_format_passages`, not merely
    intended. The preamble in `embedding_text` was written by a model; quoting it
    under a page citation would attribute those words to the source page.
    """
    if not passages:
        # No model call. There is nothing to synthesise from, and asking a model
        # to write from no evidence is how confabulation happens.
        return AgentOutcome(
            value="The documents provided do not cover this question.",
            model_calls=0,
        )

    prompt = prompts["synthesizer"]
    rendered = prompt.render(
        question=question,
        passages=_format_passages(passages, numbered=True),
        conversation_summary=conversation_summary,
    )

    try:
        raw = await _generate(rendered, settings.answering_model, settings)
        if call_log:
            call_log.record("synthesizer")
        answer = raw.strip()
        if not answer:
            raise AgentError("empty draft")
        return AgentOutcome(value=answer, model_calls=1)
    except (AgentError, httpx.HTTPError) as exc:
        logger.warning("synthesizer failed (%s); returning passages with a note", exc)
        # Degrade to handing back the evidence itself. Worse than an answer,
        # but honest and still useful — and it keeps citations valid.
        listing = "\n\n".join(f"[{i}] {p.display_text}" for i, p in enumerate(passages, 1))
        return AgentOutcome(
            value=(
                "An answer could not be composed. The most relevant passages "
                f"found were:\n\n{listing}"
            ),
            degraded=True,
            reason=f"synthesizer: {type(exc).__name__}",
            model_calls=1,
        )


# -------------------------------------------------------------------- verifier


async def verify(
    draft: str,
    passages: list[Passage],
    *,
    prompts: PromptSet,
    settings: OllamaSettings,
    call_log: CallLog | None = None,
) -> AgentOutcome[Verification]:
    """Ground every claim against the evidence; retract what is unsupported.

    Runs on `verifier_model`, which is deliberately NOT a reasoning model. This
    call was measured at 177 of 233 seconds — 76% of the whole read path — when
    it ran on one. See the note on `verifier_model` in app/shared/config.py for
    the measurements, including why the fastest candidate was rejected.

    ⚠️ The verifier receives passages ONLY, never the conversation summary.
    Memory is a record of prior turns, not evidence; admitting it as a source is
    what makes memory poisoning possible
    (doc/components/04-conversation-memory.md §6).
    """
    if not passages:
        return AgentOutcome(value=Verification(verified_answer=draft), model_calls=0)

    prompt = prompts["verifier"]
    rendered = prompt.render(draft=draft, passages=_format_passages(passages, numbered=True))

    # ⚠️ One retry on a fresh sample before degrading. `verifier_model` is
    # deliberately not a reasoning model (see the docstring above), and a small
    # non-reasoning model occasionally emits JSON with a syntax slip — a missing
    # comma, an unescaped quote — that `strict=False` does not paper over.
    # Confirmed live: a correct, well-cited answer was marked "could not be
    # verified" purely because of a malformed-JSON parse error, not because
    # anything about the answer was actually unsupported. A second sample is
    # cheap relative to showing the reader a false unverified flag.
    last_exc: Exception | None = None
    calls = 0
    for _attempt in range(2):
        try:
            raw = await _generate(rendered, settings.verifier_model, settings)
            calls += 1
            if call_log:
                call_log.record("verifier")
            data = _extract_json(raw)
            verified = str(data.get("verified_answer") or "").strip()
            if not verified:
                raise AgentError("verifier returned no answer")
            return AgentOutcome(
                value=Verification(
                    verified_answer=verified,
                    claims_retracted=int(data.get("claims_retracted") or 0),
                    claims_hedged=int(data.get("claims_hedged") or 0),
                ),
                model_calls=calls,
            )
        except (AgentError, httpx.HTTPError, ValueError) as exc:
            last_exc = exc

    logger.warning("verifier failed (%s); returning draft marked unverified", last_exc)
    # ⚠️ Marked in the ANSWER TEXT, not only on the span. A reader must not
    # be shown an unverified answer that looks identical to a verified one.
    return AgentOutcome(
        value=Verification(
            verified_answer=f"{draft}\n\n_(This answer could not be verified.)_",
            unverified=True,
        ),
        degraded=True,
        reason=f"verifier: {type(last_exc).__name__}",
        model_calls=calls,
    )


# ------------------------------------------------------------------ summarizer


async def summarize_conversation(
    previous: ConversationSummary,
    evicted: tuple[Turn, ...],
    *,
    prompts: PromptSet,
    settings: OllamaSettings,
    conversation: ConversationSettings,
    call_log: CallLog | None = None,
) -> AgentOutcome[ConversationSummary]:
    """Fold turns that aged out of the verbatim window into the summary.

    ⚠️ Runs AFTER the answer has been returned, never before it.

    The summary is not needed until the *next* question arrives, so putting this
    on the critical path would add a model call to a response the user is already
    waiting on — for a result nothing in that response uses.

    Degrades to the previous summary unchanged. That is the right fallback: the
    conversation keeps whatever it had established rather than losing it, and the
    only cost is that the evicted turns are forgotten. Losing the accumulated
    summary because one pass failed would be far worse.
    """
    if not evicted:
        return AgentOutcome(value=previous, model_calls=0)

    from app.engine.query import memory

    prompt = prompts["conversation-summarizer"]
    rendered = prompt.render(
        prior_summary=memory.render_summary(previous),
        recent_turns=memory.render_turns(evicted),
    )

    try:
        raw = await _generate(rendered, settings.answering_model, settings)
        if call_log:
            call_log.record("conversation-summarizer")
        updated = memory.summary_from_mapping(_extract_json(raw))
        # Merged rather than replaced — see merge_summaries for why a forgetful
        # pass must not be able to erase an established parameter. Then trimmed,
        # because merging alone never removes anything and the summary is
        # prepended to every later prompt.
        merged = memory.merge_summaries(previous, updated)
        bounded = memory.trim_summary(
            merged,
            max_tokens=conversation.summary_max_tokens,
            tokenizer_repo=settings.tokenizer_repo,
        )
        return AgentOutcome(value=bounded, model_calls=1)
    except (AgentError, httpx.HTTPError, ValueError) as exc:
        logger.warning("conversation summariser failed (%s); keeping the previous summary", exc)
        return AgentOutcome(
            value=previous,
            degraded=True,
            reason=f"conversation-summarizer: {type(exc).__name__}",
            model_calls=1,
        )


# ------------------------------------------------------------- crew-backed roles
#
# ⚠️ These run the SAME four roles through Crew.AI agents. What they deliberately
# do NOT do is move the guards into the framework.
#
# Parsing, the invented-scope guard, the display_text-only rule and the
# degradation paths all stay in this module and are applied to the crew's output
# exactly as they are to a direct call. That is the whole reason the crew path
# could be added without re-verifying every property from scratch: the framework
# changes who talks to the model, not what is trusted about the reply.


async def plan_with_crew(
    question: str,
    *,
    corpus_description: str,
    conversation_summary: str,
    recent_turns: str,
    prompts: PromptSet,
    settings: OllamaSettings,
    sub_question_cap: int,
    call_log: CallLog | None = None,
) -> AgentOutcome[Plan]:
    """The planner, as a Crew.AI agent. Same output contract as `plan`."""
    import anyio

    from app.engine.query import crew as crew_module

    rendered = prompts["planner"].render(
        corpus_description=corpus_description,
        conversation_summary=conversation_summary,
        recent_turns=recent_turns,
        question=question,
    )

    try:
        llm = crew_module.build_llm(settings.answering_model, settings)
        agent = crew_module.planner_agent(prompts, llm)
        raw = await anyio.to_thread.run_sync(
            crew_module.run_task,
            agent,
            rendered,
            "A JSON object with keys: intent, standalone_question, sub_questions.",
        )
        if call_log:
            call_log.record("planner")

        parsed = _parse_plan(raw, question, sub_question_cap)
        context = f"{conversation_summary}\n{recent_turns}"
        parsed = replace(
            parsed,
            sub_questions=_reject_invented_scope(
                parsed.sub_questions, question, context, corpus_description
            ),
            standalone_question=_reject_invented_scope(
                (parsed.standalone_question,), question, context, corpus_description
            )[0],
        )
        return AgentOutcome(value=parsed, model_calls=1)
    except Exception as exc:
        logger.warning("crew planner failed (%s); degrading to single-question lookup", exc)
        return AgentOutcome(
            value=Plan(intent="lookup", standalone_question=question, sub_questions=(question,)),
            degraded=True,
            reason=f"crew-planner: {type(exc).__name__}",
            model_calls=1,
        )


async def synthesize_with_crew(
    question: str,
    passages: list[Passage],
    *,
    conversation_summary: str,
    prompts: PromptSet,
    settings: OllamaSettings,
    call_log: CallLog | None = None,
) -> AgentOutcome[str]:
    import anyio

    from app.engine.query import crew as crew_module

    rendered = prompts["synthesizer"].render(
        question=question,
        passages=_format_passages(passages, numbered=True),
        conversation_summary=conversation_summary,
    )

    try:
        llm = crew_module.build_llm(settings.answering_model, settings)
        agent = crew_module.synthesizer_agent(prompts, llm)
        draft = await anyio.to_thread.run_sync(
            crew_module.run_task,
            agent,
            rendered,
            "The answer text, with every factual claim marked [n].",
        )
        if call_log:
            call_log.record("synthesizer")
        return AgentOutcome(value=draft.strip(), model_calls=1)
    except Exception as exc:
        logger.warning("crew synthesizer failed (%s); returning passages unsynthesised", exc)
        return AgentOutcome(
            value=(
                "I could not compose an answer, but these passages were retrieved:\n\n"
                + _format_passages(passages, numbered=True)
            ),
            degraded=True,
            reason=f"crew-synthesizer: {type(exc).__name__}",
            model_calls=1,
        )


async def verify_with_crew(
    draft: str,
    passages: list[Passage],
    *,
    prompts: PromptSet,
    settings: OllamaSettings,
    call_log: CallLog | None = None,
) -> AgentOutcome[Verification]:
    """⚠️ A SEPARATE agent from the synthesizer, never the same one.

    An agent carries its own context. Sharing one would mean the verifier sees
    the reasoning that produced the draft, and a model tends to accept a claim it
    has just justified. The four-agent count exists for this reason alone.
    """
    import anyio

    from app.engine.query import crew as crew_module

    if not passages:
        return AgentOutcome(value=Verification(verified_answer=draft), model_calls=0)

    rendered = prompts["verifier"].render(
        draft=draft, passages=_format_passages(passages, numbered=True)
    )

    # ⚠️ One retry on a fresh sample before degrading — see the direct path's
    # `verify()` for why: a small non-reasoning model occasionally emits
    # syntactically broken JSON, and that is a generation glitch, not a finding
    # that the answer is unsupported. Confirmed live via a crew-verifier failure
    # ("malformed JSON: Expecting ',' delimiter") on an otherwise correct answer.
    last_exc: Exception | None = None
    calls = 0
    for _attempt in range(2):
        try:
            llm = crew_module.build_llm(settings.verifier_model, settings)
            agent = crew_module.verifier_agent(prompts, llm)
            raw = await anyio.to_thread.run_sync(
                crew_module.run_task,
                agent,
                rendered,
                "A JSON object with keys: verified_answer, claims_retracted, claims_hedged.",
            )
            calls += 1
            if call_log:
                call_log.record("verifier")

            data = _extract_json(raw)
            verified = str(data.get("verified_answer") or "").strip()
            if not verified:
                raise AgentError("crew verifier returned no answer")
            return AgentOutcome(
                value=Verification(
                    verified_answer=verified,
                    claims_retracted=int(data.get("claims_retracted") or 0),
                    claims_hedged=int(data.get("claims_hedged") or 0),
                ),
                model_calls=calls,
            )
        except Exception as exc:
            last_exc = exc

    logger.warning("crew verifier failed (%s); returning draft marked unverified", last_exc)
    return AgentOutcome(
        value=Verification(
            verified_answer=f"{draft}\n\n_(This answer could not be verified.)_",
            unverified=True,
        ),
        degraded=True,
        reason=f"crew-verifier: {type(last_exc).__name__}",
        model_calls=calls,
    )
