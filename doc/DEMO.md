# Demonstration Guide

> Three recordings, in this order. Each one shows a different half of the system,
> and the order matters: a corpus has to exist before prompts can be shown
> resolving against it, and both have to exist before a chat answer means
> anything.

**Live endpoints**

| | |
|---|---|
| Chat | `http://<host>:3000` |
| Admin | `http://<host>:8000/admin` |
| Traces & prompts | `http://<host>:6006` |

Before recording anything:

```bash
curl -s http://<host>:8000/health/ready | python3 -m json.tool
```

Every dependency must read `ok`. **The `reranker` line is the one to check** — if
it is missing or degraded, retrieval has no score floor, the system cannot
decline, and every refusal in demo 3 will be fake.

---

## 1 · Ingestion

**What this proves:** a PDF becomes searchable, cited, page-anchored content —
and the system knows when it has seen a document before.

### Show

1. **The corpus as it stands.** `GET /admin` — 9 documents, chunk counts per
   document. The counts are the point: a document row with zero chunks parsed but
   indexed nothing, and the row alone would not show it.

2. **Upload a new PDF** with metadata. Only *file* and *collection* are required.
   Say why the optional fields matter while the upload runs:
   - `description` feeds the **corpus description**, which is what the planner
     reads to decide whether a question is in scope at all
   - `effective_date` drives **version supersession** — two editions of the same
     document collapse to the newer one. ⚠️ Our two Etihad editions have no
     effective date, so they *do not* collapse; that is a metadata gap, not a
     code one, and it is worth saying out loud

3. **Progress polling.** The page polls `GET /ingestion-runs/{id}`, reading from
   `ingestion_runs` in Postgres. Mention that **no Celery result backend is
   configured** — deliberately, so there is never a second, competing account of
   what a job is doing.

4. **Upload the same file again** → `200 duplicate`, nothing queued. The hash is
   computed synchronously *before* enqueueing: hashing costs under a second and
   is the one piece of work that decides whether there is any work at all.

### Say

> Parsing runs through Docling with table structure enabled. Each chunk gets a
> one-sentence contextual preamble written by a model — that preamble is what
> gets embedded, while the **original text** is what gets quoted in a citation.
> Two separate fields, never conflated: rendering the embedded text under a page
> reference would show words that do not appear on that page.

### Numbers to have ready

```
9 documents · 589 chunks · 589 preambles · run status completed
```

---

## 2 · Prompt registration through Arize Phoenix

**What this proves:** prompts are operational configuration, not code. They are
versioned, editable without a deploy, and every answer records which version
produced it.

### Show

1. **Phoenix → Prompts.** Seven prompts, each under the `production` tag:
   `planner`, `retrieval-specialist`, `synthesizer`, `verifier`,
   `chunk-contextualizer`, `document-summarizer`, `conversation-summarizer`.

2. **Version history.** The planner is on **version 8**, the synthesizer on
   **version 9** — every one of those is a real change made while fixing real
   behaviour today. Open a diff and show one. Good candidates:
   - the planner rule that forces a comparison to produce **one sub-question per
     subject** (without it, "Etihad or Delta?" searched as a single query and
     only ever retrieved one airline)
   - the synthesizer rule that says **do not pick a winner** when the documents
     do not rank things, but do set out what each says

3. **Resolution at runtime.** Show that the running system reads from the
   registry, not from disk:

   ```bash
   docker compose exec -T backend python -c "
   from app.shared.config import get_settings
   from app.shared.prompts import PromptRegistry
   ps = PromptRegistry(get_settings().phoenix).resolve_all()
   print('degraded:', ps.degraded)
   print(ps.sources)"
   ```

   `degraded: False` and every source `registry` is the proof.

4. **The three-tier fallback.** Registry → last good cache → bundled file. Stop
   Phoenix and show the system still answering, now reporting `degraded: True`
   with sources `bundled`. **An observability outage must not become an
   availability outage** — but it must be visible, which is why `degraded` is on
   the span and in the readiness report.

### Say

> ⚠️ Two failures here were only found by looking rather than trusting a status.
> `push_bundled` originally compared *existence* rather than *content*, so once a
> prompt existed under the tag every later edit was silently skipped — the
> registry served version 1 forever while the file on disk showed the change.
> Editing a prompt appeared to do nothing.

---

## 3 · Chat and retrieval workflow

**What this proves:** the agentic read path — planning, retrieval with retry,
synthesis, verification — and, most importantly, that the system refuses when it
should.

Use the **`agentic-rag`** profile (5 passages). `agentic-rag-fast` trades
evidence for latency and is weaker on comparisons.

### Show, in this order

| # | Question | What to point at |
|---|---|---|
| 1 | *"what is the excess baggage charge?"* | A cited answer. Expand a source: **page number, section heading, and the document's own words.** None of it is model output — only the number `[1]` comes from the model, and even that is validated |
| 2 | *"how do I bake sourdough bread?"* | **~2 seconds, one model call, zero searches.** The planner short-circuits before touching the database |
| 3 | *"what is the surfboard fee?"* | ⚠️ **The most important one.** A corpus full of baggage and sports-equipment rules, but no surfboard fee. Retrieval *does* return plausible passages — and the system declines anyway, offering them as "closest matches, these do **not** answer the question" |
| 4 | *"whose baggage services are better, Etihad or Delta?"* | Both airlines cited, **no winner picked**, closing with "the comparison is the reader's to make". Ranking would mean inventing a criterion no document supplies |
| 5 | Follow-up: *"what about delayed baggage?"* | Conversation memory resolving a reference with no subject in it |

### Then show the trace

Phoenix → the `rag-serving` project. Open the root span of question 3 and show
the decision attributes:

```
intent · sub_question_count · model_calls · searches_used · retries_used
claims_retracted · provenance · passages_used · citations · degraded
```

> These are on the **root** span deliberately. Several agentic behaviours fail
> into *inertness* while still producing well-formed answers — a planner that
> always says `lookup`, a retry loop that never fires, a verifier that never
> retracts. Each looks healthy per request. Only the distribution across many
> requests reveals it, and attributes on a child span would be invisible to the
> query that detects it.

### Then show the evaluation

```bash
docker compose exec -T backend python -m eval.cli --collection corpus
```

Point at three things:

- **`out_of_scope` 100% declined, `near_miss` 100% declined** — reported
  separately, never averaged. A single figure would hide the one that matters
- **Decision distributions all vary** — the run FAILS with a named assertion if
  any one is constant, which is the only detector for an inert agent
- **All four multi-turn cases pass** — including a seeded conversation where a
  previous turn asserts a baggage allowance that appears nowhere in the corpus.
  The follow-up must not repeat it. **Single-turn evaluation cannot detect this**,
  and single-turn evaluation is how RAG systems are normally graded

---

## What to say about the things that are not perfect

Being straight about these is stronger than hoping nobody asks.

| Limitation | The honest framing |
|---|---|
| **26–34s per answer** (p50 ~17s on GPU) | Three sequential model calls on local hardware. Streaming would make it *feel* faster; it would not be faster |
| **Generic queries retrieve boilerplate** | "What are X's baggage policies?" finds the glossary; "X checked baggage allowance" finds the clause. Phrasing matters more than it should |
| **`keep_floor = 0.3` is unfitted** | Every decline depends on it and it has never been calibrated on held-out data. The 100% results are real but rest on an arbitrary threshold |
| **Feedback is stranded** | Thumbs-up/down lands in OpenWebUI's own database, disconnected from our traces. The OpenAI-compatible contract has no feedback channel — a known cost of adopting a standard |
| **Two Etihad editions do not collapse** | Version supersession works; the documents simply have no `effective_date`. A metadata gap |
