# OpenWebUI

> Baseline item 10 of 10 — *"OpenWebUI (Docker deployment) — Chatbot interface connected to your
> backend API"*.
>
> **This document is the authority on the frontend integration** — design choice 5.9.
>
> The interface itself is given, not built. The work is connecting it, and deciding what it may and
> may not do.

---

## 1. What it is

A ready-made chat interface, deployed as a container.

### Scope

| Ours | Theirs |
|---|---|
| The API contract it talks to | The chat UI, history, rendering |
| Deciding what it may and may not do | Authentication, settings, themes |

Nothing about the interface is built here. Everything below concerns the boundary.

---

## 2. Use-cases covered

| # | Use-case | Purpose |
|---|---|---|
| 1 | Chat interface | The thing users type into |
| 2 | Conversation history and rendering | Provided; we supply content it can render |
| 3 | Model selection | Repurposed as profile selection — see §7 |
| 4 | Streaming display | Renders progress and answer as they arrive |

---

## 3. The integration decision

| Option | Assessment |
|---|---|
| **OpenAI-compatible API** | **Chosen** — expose `/v1/chat/completions` and `/v1/models`; the interface registers the backend as a provider |
| Pipelines / Functions | Plugin code, coupled to this interface specifically |
| Ollama-compatible API | Works, but a narrower contract |
| Fork the frontend | No |

**Why OpenAI-compatible:**

- No plugin code to write or maintain
- Streaming semantics are defined by the specification rather than invented
- **The backend stops being locked to this frontend.** Any compatible client can drive it, including
  `curl` for testing — which matters more than it appears, because it means the integration can be
  verified without the interface running at all

Design choice 5.1 (the API contract) therefore becomes a standard we adopt rather than something we
design and then have to defend.

The cost is that the standard schema has no natural slot for several things this system needs. Four
of them, below.

---

## 4. Problem — citations

The schema has no citation field.

**Render citations as markdown in the message content.** This works in every client, survives any
renderer, and degrades gracefully to plain text.

Collapsing the supporting passage uses HTML embedded in markdown:

```markdown
<details><summary>[1] Conditions of Carriage · p.14 · 4.2 Exclusions</summary>
"Cover does not apply where the property has been left unoccupied…"
</details>
```

⚠️ Conditional on the renderer permitting raw HTML — **verify against the pinned image**. The
fallback if not is a plain list with the passage as a blockquote: verbose, but never broken.

A richer structured field may be emitted alongside for clients that understand one, but the markdown
is the contract. Anything that depends on a non-standard field being honoured is a dependency on this
particular interface, which the integration decision was specifically avoiding.

---

## 5. Problem — progress, and the streaming conflict

Streamed deltas are **append-only**. There is no way to show *"Searching…"* and then replace it.

And a prior constraint compounds it: **verification happens after drafting**, so streaming the draft
means streaming text the verifier may retract. For a policy-lookup tool, showing a claim and then
withdrawing it is worse than a pause.

### The solution both problems share

**Emit progress as reasoning content.**

```
<think>
Planning…
Searching documents…
Found 5 passages
Composing answer…
Checking against sources…
</think>

The checked baggage allowance is 23 kg [1].
```

Clients — including this one — render reasoning blocks **collapsed**. So:

- The user sees continuous activity while the request runs
- The block folds away once the answer arrives
- Message history is not polluted with status lines
- Nothing is displayed that might subsequently be retracted
- No plugin is required, and it works in any client handling reasoning content

The answer itself streams normally, after verification completes.

### What this does and does not fix

It does not make anything faster. It makes a wait of tens of seconds **legible** rather than
indistinguishable from a hang — which is the difference between a demonstration that works and one
that appears broken.

---

## 6. Problem — conversation identity

The standard contract is **stateless**: each request carries the full message array, and there is no
conversation identifier.

Our memory design is server-side — rolling summaries, provenance tags, the poisoning guards. None of
that can be reconstructed from a raw message array on each turn.

**Accept an optional conversation identifier; fall back to hashing the first user message.**

The first message does not change across a conversation, so its hash is stable — giving server-side
memory a key without requiring cooperation from the frontend.

⚠️ Two users opening with an identical first question would collide. A user identifier resolves it
where one is available; for a single-user demonstration the risk is acceptable, and it is recorded
here rather than left to be discovered.

---

## 7. Problem — profile selection

`/v1/models` must list something for the interface to offer.

**Expose the profiles as models:**

```
agentic-rag         qwen3:8b, 5 chunks   — quality
agentic-rag-fast    qwen3:4b, 3 chunks   — speed
```

Both generative models are resident during serving, so switching profiles triggers no model load.
Verification runs on the small model in both.

The model picker becomes the profile switch. No custom interface is needed, and it makes the latency
trade-off visible and switchable during a demonstration.

---

## 8. ⚠️ What must be disabled

The part that is easy to overlook and embarrassing to discover live.

| Feature | Why it must be off |
|---|---|
| **Built-in document upload / retrieval** | A user uploads a PDF, the interface runs *its own* retrieval, and every mechanism built here is bypassed — silently, producing worse answers with no indication which path was taken |
| **Direct model-host connection** | It would reach the model host directly, skipping the agents entirely |
| Web search | Answers would stop being grounded in the corpus |

The first is the dangerous one. **The interface is perfectly capable of doing a mediocre job of
exactly what this backend does**, and nothing in the output signals which path an answer came from.
A demonstration could run entirely on the wrong pipeline without anyone noticing.

### Document upload

Upload is deliberately **not** available through the chat interface. It is a backend operation —
performed by an operator against `POST /documents`, not by end users mid-conversation.

This is a scope decision rather than an omission: the system is a document *search* platform over a
curated corpus. Someone administers the corpus; users query it. End users adding their own documents
would be a different product, with access-control questions this one does not answer.

**And it could not be routed through this interface even if the scope allowed it.** Its upload control
is not a generic one — it is wired into its own extractor, chunker and vector store, with no hook that
redirects the file to an external URL and no extension point for a custom page; Tools and Functions
act on chat turns, not on file management. A file uploaded here enters *its* index and this pipeline
never sees it.

The operator surface is therefore served by the backend at `GET /admin` — see
[11 · FastAPI](11-fastapi.md) §6.

---

## 9. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Integration mechanism | **OpenAI-compatible API** | No plugin code; the backend is not locked to this frontend |
| Citations | Markdown in the message content | Survives any renderer; a structured field would be a dependency on this interface |
| **Progress display** | Emitted as **reasoning content** | Renders collapsed; append-only streaming cannot replace status text |
| Streaming the draft | **Not done** — the verified answer streams | Streaming a draft means streaming text that may be retracted |
| Conversation identity | Optional identifier, falling back to a hash of the first user message | The standard contract is stateless; server-side memory needs a key |
| Identity collision | Accepted for single-user use, recorded | Identical opening questions from different users would collide |
| Profile selection | Exposed as two models in `/v1/models` | The model picker becomes the profile switch |
| **Built-in retrieval** | **Disabled** | It would silently bypass the entire pipeline |
| Direct model-host connection | Disabled | It would skip the agents |
| Web search | Disabled | Answers must stay grounded in the corpus |
| Document upload via chat | Not offered | Corpus administration is an operator task, not an end-user one |
| Operator upload surface | Served by the backend at `GET /admin` | This interface has no extension point that keeps a file out of its own retrieval |

### ⚠️ Discovered in implementation: generation features must also be disabled

Not in the original list, and the most expensive omission. This interface fires
**extra model requests the user never asked for** — to name a chat, tag it,
autocomplete it, generate a retrieval query. Against an ordinary chat model each
is a cheap side call. Against this backend **each is a full agentic run**:
planning, retrieval, verification — and, because conversation identity derives
from the first user message, each writes a *spurious conversation* into
server-side memory alongside the real one.

    ENABLE_TITLE_GENERATION, ENABLE_TAGS_GENERATION, ENABLE_AUTOCOMPLETE_GENERATION,
    ENABLE_RETRIEVAL_QUERY_GENERATION, ENABLE_SEARCH_QUERY_GENERATION  -> all false

The image is also **pinned to `v0.6.5`** rather than `:main`. The open questions
below are answered by verification against a specific image, and a moving tag
makes those answers expire silently — the first sign being a demo where citations
render as literal `<details>` tags.

### Also discovered: a question about the system is not a question for the documents

Asked *"what type of questions can I ask you?"*, the pipeline planned an ordinary
lookup, searched a corpus of baggage rules, found nothing above the floor, and
refused **after five minutes** with three irrelevant "closest matches". Every
stage behaved correctly; the question was never a retrieval question.

The read path now has a `capability` intent that answers from the corpus
description with **no search and no further model call** — ~2 s instead of five
minutes. See `Intent` in `app/shared/types.py`.

### Still open

| Item | Settled by |
|---|---|
| Whether v0.6.5 renders reasoning blocks collapsed as expected | Verification against the running image — SSE chunks confirmed by `curl`, not yet seen rendered |
| Whether raw HTML in markdown (`<details>`) is honoured | Same |
| Whether a user identifier is available to disambiguate conversations | Inspection of what the interface sends |
