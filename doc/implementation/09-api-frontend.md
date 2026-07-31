# API & Frontend — Build Record

> The ninth phase of the build: the engine becomes something a person can use —
> a chat interface for readers, an upload page for whoever curates the corpus.
>
> This document records what was built, how each behaviour was *verified* rather
> than assumed, and what went wrong along the way. The design documents say what
> should be built; this says what was.

---

## 1. What this phase covers

Satisfies baseline item 10 (**OpenWebUI**) and puts every earlier phase behind an
HTTP surface.

| Area | Delivers | Status |
|---|---|---|
| Chat | `POST /v1/chat/completions`, streaming and not | ✅ |
| Models | `GET /v1/models` — the two profiles | ✅ |
| Progress | Stage events as reasoning content | ✅ |
| Identity | `X-Conversation-Id`, or a hash of the opening message | ✅ |
| Upload | `POST /documents` with synchronous duplicate detection | ✅ |
| Curation | List, delete, run progress | ✅ |
| Admin page | One self-contained file, no external requests | ✅ |
| Background work | Celery task, ingestion queue at concurrency 1 | ✅ |
| Frontend lockdown | Built-in retrieval, web search, direct model host, extra generations | ✅ |

### Explicit non-goals

- **No authentication.** The stack binds to localhost; recorded as open in
  [11 · FastAPI](../components/11-fastapi.md) §6
- **No engine logic in any route.** Every route translates HTTP and calls an
  ordinary function

---

## 2. Adopting a contract instead of designing one

Exposing an OpenAI-compatible API means no plugin code exists anywhere, streaming
semantics come from a specification rather than from us, and — the part that
mattered most while building — **the backend is not locked to one frontend**.
Every verification below was done with `curl`, without the interface running.

The standard schema has no slot for three things this system needs, and each is
solved without a non-standard field that would recreate the coupling:

| Need | Solution |
|---|---|
| Citations | Markdown in the message content, collapsed with `<details>` |
| Progress | Reasoning content, which clients render collapsed |
| Conversation identity | A header, falling back to a hash of the first user message |

### The draft is never streamed

Verification runs *after* drafting and may retract claims. Streaming the draft
would show a claim appearing and then vanishing — worse, for a policy-lookup
tool, than a pause. Only text that survived verification is sent, and the wait is
filled with progress instead.

---

## 3. Components: steps executed and how each was verified

### 3.1 The chat endpoint, end to end

```
$ curl -sN -X POST localhost:8000/v1/chat/completions -d '{"stream":true,...}'

PROGRESS (rendered collapsed):
    Planning the search
    Searching: What is the rate for excess baggage?
    Found 2 passage(s); composing the answer
    Checking every claim against the sources

ANSWER:
The excess baggage charge is explained on the company's Website [1].

---
**Sources**
<details><summary>[1] etihad-general-conditions-of-carriage · p.35 · 8.2 EXCESS BAGGAGE</summary>
> 8.2.1 You will be required to pay a charge for carriage of Baggage in excess…
</details>
```

### 3.2 Identity without cooperation from the frontend

The standard contract is stateless. Server-side memory needs a key, so an
explicit `X-Conversation-Id` wins and otherwise the id is a hash of the first
user message — stable because that message does not change as a conversation
grows. Asserted directly: a one-message array and a three-message array
continuing it resolve to the same id.

The client's resent history is deliberately **ignored**; only the latest user
message is answered. Memory already holds a curated version of the conversation,
and feeding the client's raw copy in as well would put unfiltered assistant
output back into context — undoing the provenance tagging and bounding that
[07 · Conversation Memory](07-conversation-memory.md) exists to provide.

### 3.3 Upload: hash first, queue second

```
$ curl -F file=@delta-contract-of-carriage-domestic.pdf -F collection=corpus …
{"status": "duplicate", "detail": "identical content is already indexed"}
```

Hashing costs well under a second and is the one piece of work that decides
whether there is any work. Queueing first would mean paying for a full parse —
minutes of model time — to discover the document was already indexed.

A broker that cannot be reached produces **503, never 202**: a 202 says "accepted,
work will happen", and returning one for a message that was never queued leaves
an operator watching a run that will never start.

### 3.4 Curation

```
$ curl 'localhost:8000/documents?collection=corpus'
corpus documents: 9
    127 chunks  delta-contract-of-carriage-international.pdf
    115 chunks  etihad-axa-travel-insurance-policy.pdf
     96 chunks  delta-contract-of-carriage-domestic.pdf
     …
```

Chunk counts are shown because they are the operator's signal that ingestion
actually produced something — a document row with zero chunks parsed but indexed
nothing, and the row alone would not show it.

### 3.5 The frontend lockdown

The criterion that is embarrassing to discover live: **this interface is
perfectly capable of doing a mediocre job of exactly what this backend does**,
and nothing in the output signals which path an answer took.

| Disabled | Why |
|---|---|
| Built-in retrieval, web search, local fetch | Would silently bypass the entire pipeline |
| Chat file upload | The file would enter *its* index and this pipeline would never see it |
| Direct model-host connection | Would skip every agent |
| **Title, tag, autocomplete, query generation** | See below |

The last row was not in the design and is the one worth recording. These fire
extra model requests the user never asked for. Against an ordinary chat model
that is a cheap side call; against this backend **each one is a full agentic
run** — planning, retrieval, verification — and, because conversation identity
derives from the first user message, each writes a *spurious conversation* into
memory alongside the real one.

The image is now pinned to `v0.6.5` rather than `:main`. The design's open
questions — whether this version renders reasoning collapsed, whether it honours
raw HTML in markdown — are answered by verification against a specific image, and
a moving tag makes those answers expire silently.

---

## 4. Challenges and how they were resolved

### 4.1 ⚠️ The container had no cross-encoder, and therefore could not decline

The backend crashed on startup with `ModuleNotFoundError: No module named
'docling'` — and fixing that revealed something far worse in the logs:

```
WARNING  rerank: sentence-transformers not installed; re-ranking disabled
WARNING  tracing: phoenix.otel not installed; tracing disabled
WARNING  prompts: phoenix client not installed; bundled files only
```

The Dockerfile installs optional dependency groups through an `EXTRAS` build arg,
defaulting to empty, on a deliberate plan: install each group in the epic that
needs it, so a base image stays small and a dependency problem surfaces in the
epic that introduced it. The plan was sound. What was missing was **anything that
failed when an epic finished and its extra was never added to Compose.**

So the running backend had no cross-encoder, therefore no score floor, therefore
**retrieval could never return an empty result** — and an empty result is the
entire mechanism behind declining rather than confabulating. The system kept
answering; it had simply lost the ability to say "the documents do not cover
this".

It was invisible from outside. `/health/ready` returned 200, answers came back
fluent and cited, and the only trace was one WARNING at startup. **The container
ran that way for 39 hours.**

Every live verification in records 05–08 was run on the host via `uv run`, where
the extras are installed — which is why the behaviour was real there and absent
here, and why nothing caught it.

**Fixes, both needed.** Compose now passes
`--extra ingest --extra retrieval --extra observability`; and `/health/ready`
gained a `reranker` probe that reports **unavailable**, not a warning, when the
cross-encoder cannot be loaded. Degrading quietly is defensible; degrading
quietly *while reporting ready* is not.

### 4.2 ⚠️ Answering fluently from the fixture corpus

The first working chat request returned a confident, cited answer about
*"route ABZ-LHR"* citing *"Baggage Policy · p.3"* — a document nobody had ever
uploaded.

The endpoint read `settings.ingestion.default_collection`, which is `"default"` —
also the name of the seeded **fixture** collection: 17 synthetic chunks about a
fictional airline, sitting in the same table as the 589 real ones.

```
 collection | count
------------+-------
 default    |    17     <- fixtures, and what the API was serving
 corpus     |   589     <- the actual corpus
```

Nothing about the response looked wrong. It had citations, page numbers, section
headings and a plausible answer. The only tell was recognising the document name.

**Fix.** `retrieval.collection`, its own setting defaulting to `"corpus"`.
Ingesting and serving are different concerns and now have different knobs, with a
test asserting the two are not equal.

### 4.3 A warm-up that made the frontend look broken

Warming the cross-encoder at startup — the item deferred here from
[06](06-agentic-read-path.md) — was implemented as a blocking call before the
lifespan's `yield`. On a cold HuggingFace cache that is a 2.2 GB download:

```
READY after 300s
```

FastAPI accepts no connections until the lifespan yields. OpenWebUI fetches the
model list once at boot, got nothing, cached an empty list, and showed **"No
results found"** in its model picker long after the backend was healthy. A slow
dependency had become a frontend that appeared broken.

**Fix.** The warm-up now runs in a task group *around* the `yield`, so the app
serves immediately and the model loads alongside it — `READY after 2s`. A query
arriving during the window pays part of the load, exactly as it would have with
no warm-up at all.

The weights also went into the container's writable layer, so every rebuild
re-downloaded them. `HF_HOME` is now set explicitly — not inherited from `$HOME`,
which differs by base image — and backed by a named volume.

### 4.4 A helper that dragged a PDF parser into the API

`hash_file` lived in `engine/ingest/parse.py`, whose module-level `import
docling` came with it. The upload endpoint needed the hash; the backend image
does not install parsing, because parsing is the worker's job.

The dependency was never real — hashing a file is `hashlib` and nothing else —
but **a function's imports travel with it**, so where a helper lives decides what
its callers are forced to install. Moved to `app/shared/hashing.py`, re-exported
from `parse` so existing callers are unaffected.

### 4.5 A NULL parameter Postgres could not type

`GET /documents` returned 500 with `AmbiguousParameter: could not determine data
type of parameter $1`. The optional filter `WHERE (:collection IS NULL OR …)`
gives Postgres no way to infer the parameter's type when it is NULL. Fixed with
an explicit `cast(:collection AS text)`.

---

## 5. Final state

| Behaviour | Verified by |
|---|---|
| Both profiles listed and selectable | Live `GET /v1/models` |
| Streaming emits progress then the verified answer | Live SSE, `[DONE]` terminated |
| Citations render collapsed with page and section | Live chat request |
| A decline offers closest matches, never sources | Live chat request |
| Identity is stable across a growing conversation | Unit test |
| The client's resent history is ignored | Unit test |
| A duplicate returns 200 and queues nothing | Live upload + unit test |
| An unreachable broker returns 503 | Unit test |
| The admin page makes no external requests | Unit test asserting no `http://` in the file |
| The reranker's absence makes the service unready | Readiness probe |

**177 tests pass** — the whole suite, unit and integration, including live models
and a live Phoenix. `mypy app tests` is clean; `ruff` is clean.

### Still open

- **A planner qualifier cost a correct answer, live.** Asked "what is the excess
  baggage charge?", the planner searched for "the rate for excess baggage **per
  kilogram**" — a qualifier the user never stated — and the clause that answers
  it (p.35, 8.2 EXCESS BAGGAGE) fell below the score floor and was returned as a
  *near miss* instead of a source. This is the known lowercase-qualifier
  limitation from [06](06-agentic-read-path.md) §4.2, now with a concrete cost
  attached: the invented-scope guard catches document and organisation names, not
  adjectives
- **Reasoning-content rendering is unverified in the interface itself.** The SSE
  chunks carry `reasoning_content` and were confirmed by `curl`; whether v0.6.5
  renders them collapsed, and whether it honours `<details>` in markdown, needs
  eyes on the pinned image
- **Context-budget enforcement**, deferred here from
  [07](07-conversation-memory.md), is **not** built. Memory yields to evidence
  because memory is bounded and small, not because anything measures the
  assembled total
- **No authentication**, by design for a localhost demonstration

---

## 6. What this unblocks

Evaluation needs a live endpoint and now has one. The deliverables phase needs
the stack to come up with one command.

---

## 7. Command reference

```bash
make up                       # whole stack; backend ready in ~2s

curl localhost:8000/v1/models
curl -sN -X POST localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"agentic-rag","stream":true,"messages":[{"role":"user","content":"..."}]}'

curl -F file=@doc.pdf -F collection=corpus localhost:8000/documents
curl 'localhost:8000/documents?collection=corpus'

open http://localhost:8000/admin      # operator surface
open http://localhost:3000            # chat interface
```
