# Arize Phoenix

> Baseline item 8 of 10 — *"Arize Phoenix — Prompt lifecycle management and observability"*.
>
> **This document is the authority on prompt storage and trace design.** Other components state what
> prompts they need and what should be observable; how those are stored, versioned and recorded is
> decided here.
>
> Satisfies requirements 3.2 and 3.3, and baseline sub-requirements 8(a), 8(b) and 8(c).

---

## 1. What it is

A self-hosted platform for observing and managing language-model applications, running as a container
in the stack.

It does **two distinct jobs**, and the brief unusually spells out both — which signals where
submissions typically fall short:

> **(a)** Initialize prompts in Arize Phoenix
> **(b)** Retrieve prompts from Arize when needed in the code
> **(c)** Use Arize for tracing, debugging, and observability

Only (c) is satisfied by adding a trace exporter. Items (a) and (b) mean prompts genuinely live here
and are fetched at runtime — the same thing requirement 3.3 asks for.

### Scope

| Owns | Does not own |
|---|---|
| Prompt storage, versioning, promotion | Durable operational state — the database holds that |
| Trace collection and inspection | Metric computation — evaluation does that |
| Per-call latency, tokens, inputs, outputs | |

The split with the database is **telemetry here, durable state there**. The join between them is
`trace_id`, stored on each message.

---

## 2. Use-cases covered

### Prompt management

| # | Use-case | Requirement |
|---|---|---|
| 1 | Push prompts at startup, idempotently | 8(a) |
| 2 | Fetch by name and tag at runtime | 8(b) |
| 3 | Version and promote without redeployment | 3.3 |
| 4 | Fall back to bundled defaults when unreachable | Availability |
| 5 | Record which prompt version produced an answer | Reproducibility |

### Tracing

| # | Use-case | Requirement |
|---|---|---|
| 6 | Auto-instrument retrieval and orchestration | 8(c), 3.2 |
| 7 | Manual spans for ingestion stages | 3.2 |
| 8 | One trace per request, spanning every stage | 8(c) |
| 9 | **Record decision attributes** — intent, sub-question count, retries, retractions | The health check |
| 10 | Link traces to stored conversation turns | Debugging |

Use-case 9 carries over from the orchestration design: four agentic behaviours can fail into
inertness while still producing well-formed output, and examining the *distribution* of decisions is
the only detector. Recording them as span attributes makes that analysis possible here directly.

---

## 3. The prompt inventory

What must exist in Phoenix, accumulated across every component:

| Prompt | Used by | Notes |
|---|---|---|
| `chunk-contextualizer` | Ingestion, per chunk | Runs thousands of times; the most consequential to get right |
| `document-summarizer` | Ingestion, per document | Skipped when the operator supplied a description |
| `conversation-summarizer` | After each response | Structured-field output |
| `planner` | Read path | Classify, rewrite and decompose in one structured output. Takes the corpus description as an input |
| `retrieval-specialist` | Read path | Includes reformulation strategies |
| `synthesizer` | Read path | Carries the four citation instructions |
| `verifier` | Read path | Sees evidence and draft only |

**Seven prompts.** Each is design choice 5.8, and each is versioned independently — revising the
contextualiser does not touch the verifier.

---

## 4. Where it fits

```
STARTUP          push all seven prompts, idempotently        [#1]
                 warm the prompt cache

REQUEST          resolve prompts by name + tag (cached)      [#2]
                 every model call traced                     [#6, #8]
                 decision attributes on the root span        [#9]
                 trace_id stored with the turn               [#10]

INGESTION        manual spans, separate project              [#7]
```

---

## 5. Prompt lifecycle

### Initialization, and a conflict worth resolving

Requirement 8(a) says push prompts at startup. The naive implementation pushes all seven every time
the container starts, creating a new version on each restart — version history fills with noise, and
a genuine edit becomes indistinguishable from a redeploy.

So push **only when content differs**. Which surfaces the real problem:

> Someone edits a prompt in the Phoenix interface. The application restarts. The bundled version
> differs. Does the push overwrite their edit?

| Approach | Consequence |
|---|---|
| Code authoritative — always push and promote | Interface edits silently overwritten; defeats the entire point |
| Phoenix authoritative — push only if absent | Code changes never reach Phoenix; bundled prompts drift into fiction |
| **Push a new version, leave the tag** | Both visible; a human decides which is live |

**The third.** Bundled prompts are a *seed and a fallback*, not the truth. On startup a differing
prompt is pushed as a new version, and the production tag stays where it is.

One exception: **if no production tag exists** — a fresh deployment — the first push sets it.
Otherwise the system starts with no live prompts at all.

### Fetch by tag, never by version number

Code asks for `synthesizer @ production`, not `synthesizer v7`.

The difference is the whole feature. With a version number, promoting v8 requires a code change and a
redeployment — exactly what externalising prompts was meant to eliminate. With a tag, promotion is a
Phoenix operation.

### ⚠️ The caching tension

Fetching per call means a network hop on each of seven stages, every request — latency, and a hard
dependency in the hot path. Caching means a promoted prompt does not take effect, which undercuts the
point.

**A TTL cache of around 60 seconds.** A minute's delay before a change goes live is acceptable for
iteration; a round trip per stage is not. A refresh fetches all seven together rather than
individually.

#### Pin the version per request

If the cache refreshes mid-request, the planner runs on v3 and the verifier on v4 — an answer
produced by a combination of versions that never existed as a set. Inconsistent, and impossible to
reproduce.

**Resolve all prompts once at request start and use that snapshot throughout.** The cache serves the
snapshot; the request never re-reads it.

### When Phoenix is unreachable

```
1. Serve the last cached version        ← usually fine; the TTL merely expired
2. Otherwise serve the bundled default
3. Log loudly
```

An observability outage must not become an availability outage.

⚠️ Note what "log loudly" means here: **the tracing system is the thing that is down**, so the log
cannot go there. It goes to stdout and to a health endpoint — because behaviour has silently changed.
The system is now running bundled prompts that may differ from what was promoted, and nothing about
the answers will look different.

### Record which versions produced the answer

The resolved versions go **two places**:

| Where | Why |
|---|---|
| The trace, as root-span attributes | Where you debug |
| The message row, as a compact map | Where you look when traces are purged, or when Phoenix was down |

```
prompt_versions: { planner: 3, retrieval-specialist: 2, synthesizer: 7, verifier: 2 }
```

The same reproducibility argument as recording ingestion configuration: *"the answers changed"* is
unanswerable without *"which prompts were live."* Cheap to store, impossible to reconstruct later.

### ⚠️ Two classes of prompt

The seven do not share a lifecycle, and treating them uniformly would be a mistake:

| Class | Prompts | Effect of promoting |
|---|---|---|
| **Serving** | planner, retrieval-specialist, synthesizer, verifier, conversation-summarizer | Takes effect on the next request |
| **Ingestion** | chunk-contextualizer, document-summarizer | **Invalidates the preamble cache and requires re-ingestion** |

The preamble cache key already includes the prompt version, so this falls out of an existing design
rather than being a new constraint. But the consequence matters: **promoting an ingestion prompt is
not a live change, it is a corpus rebuild.**

Serving prompts can be promoted freely and iterated in minutes. Ingestion prompts cost hours. Worth
marking the two classes distinctly in Phoenix so the difference is visible to whoever promotes them.

---

## 6. Trace design

### One trace per request

```
request
├── resolve prompts
├── load memory
├── planner
├── retrieval · sub-question 1
│   ├── search
│   │   ├── embed query
│   │   ├── vector search
│   │   ├── keyword search
│   │   ├── fuse
│   │   └── rerank
│   └── judge          (only if ambiguous)
├── retrieval · sub-question 2
├── dedupe + number
├── synthesizer
├── verifier
└── assemble citations
```

Auto-instrumentation supplies the model calls and retrieval operations. We add the spans marking
**our** boundaries — the fan-out, deduplication, citation assembly — because that is where our logic
lives and auto-instrumentation cannot see it.

### Attributes: decisions belong on the root span

**Root span:**

```
conversation_id, turn_index
intent                  lookup | comparison | summary | out_of_scope
sub_question_count      1–4
retries_used            total across sub-questions
claims_retracted        count
declined                true | false
prompt_versions         { planner: 3, synthesizer: 7, ... }
```

Putting these on the **root** rather than burying them in children is what makes them queryable —
*"show every trace where retries_used > 0"*, or charting the distribution of `sub_question_count`
across a run. Three levels down in a child span they are readable one trace at a time and useless in
aggregate.

That aggregate view is the only detector for agents which run correctly and decide nothing.

**Retrieval spans:** the reformulated query, attempt number, `top_score`, the signal, passage count.

**Rerank span:** candidates in, kept out, score range.

### ⚠️ Payload discipline

Evidence runs to roughly 4,000 tokens per request. Attaching full passage text to every span makes a
single trace tens of kilobytes — multiplied by every retrieval attempt, every request.

| Span | Carries |
|---|---|
| Retrieval, rerank | **Chunk IDs and scores only** |
| Synthesizer input | **Full text** — the one that must be inspectable |
| Everything else | Truncated past a threshold |

The synthesizer's input is the exception worth paying for: when an answer is wrong, *"what did the
model actually see"* is the first question, and identifiers alone cannot answer it.

### ⚠️ Ingestion will flood it

Thousands of contextualisation calls, each a span. Traced naively, one ingestion run buries every
request trace under thousands of siblings.

**Send ingestion to a separate project.** Phoenix supports multiple projects, so `ingestion` and
`serving` never mix — request traces stay findable and ingestion traces stay inspectable on their own
terms.

Within the ingestion project:

- **One span per document**, carrying aggregate counts — chunks produced, preambles generated versus
  cached, duration
- **A sample of chunk-level spans** — enough to debug a poor preamble, not enough to drown in

The document-level aggregates answer the questions that actually get asked: *"which document took
forty minutes"* matters; *"what happened to chunk 3,847"* rarely does.

### Recording degradation

**Fail-forward makes failures invisible.** That is its purpose — the request succeeds rather than
erroring. But it means nothing surfaces unless the trace says so explicitly.

Every span must record:

- Which fallback was taken, if any
- Parse retries attempted
- Timeouts hit
- Whether the answer went out unverified

Without this, a system whose planner falls back on every request looks entirely healthy: answers
appear, no errors are raised, and the degradation becomes a silent permanent state.

The same category of problem as inert agents, arriving from the opposite direction.

### Retention

Traces grow without bound, and the two projects have different useful lifespans:

| Project | Valuable for |
|---|---|
| `serving` | Weeks — debugging recent behaviour, comparing before and after a change |
| `ingestion` | Days — useful around the run, rarely after |

Shorter retention on ingestion, longer on serving. A position is needed before storage fills, not
after.

---

## 7. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Scope | Prompts and telemetry | Durable state belongs to the database; metrics to evaluation |
| Prompt count | Seven, versioned independently | |
| Push on startup | Only when content differs | Unconditional pushing fills history with restart noise |
| Push conflict | **New version, tag unmoved** | Bundled prompts are a seed and fallback, not the truth |
| Fresh deployment | First push sets the production tag | Otherwise nothing is live |
| Runtime resolution | By **tag**, never version number | A version number reintroduces the redeployment that externalising removed |
| Caching | TTL ~60s, all seven refreshed together | Balances hot-path latency against promotion delay |
| **Version pinning** | Resolved once at request start | Otherwise stages run versions of a set that never existed together |
| Phoenix unreachable | Last cached → bundled default → log to stdout and health endpoint | Cannot log to the system that is down |
| Version recording | Root-span attributes **and** message row | Traces may be purged; Phoenix may have been down |
| **Prompt classes** | Serving vs ingestion, marked distinctly | Promoting an ingestion prompt is a corpus rebuild, not a live change |
| Trace granularity | One trace per request | |
| Manual spans | Our orchestration boundaries | Auto-instrumentation cannot see them |
| **Decision attributes** | On the **root span** | Buried in children they are unqueryable, and aggregation is the point |
| Retrieval span payload | Chunk IDs and scores only | Full text everywhere makes traces tens of KB each |
| Synthesizer span payload | Full input text | *"What did the model see"* is the first debugging question |
| **Ingestion traces** | **Separate Phoenix project** | Otherwise one run buries every request trace |
| Ingestion granularity | Per document, plus sampled chunk spans | Document aggregates answer the useful questions |
| Degradation recording | Fallbacks, retries, timeouts, unverified flag — explicit | Fail-forward means failures never surface as errors |
| Retention | Shorter for ingestion, longer for serving | Position needed before storage fills |

### Still open

| Item | Settled by |
|---|---|
| Prompt text for all seven — design choice 5.8 | Evaluation, with prompts held here |
| Retention periods for both projects | Not yet discussed; parallels the database's open retention question |
| Chunk-span sampling rate during ingestion | Measurement once ingestion volume is known |
| Whether a 60-second prompt TTL is the right balance | Experience during prompt iteration |
