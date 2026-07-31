# Contextual Agentic RAG

> Baseline item 3 of 10 — *"Contextual Agentic RAG (Anthropic-style) — Embeddings / LLM / Re-ranking"*.
>
> Not a tool but a pair of techniques. Serves requirements 2.1, 2.4 and 2.5.

---

## 1. What it is

Baseline item 3 bundles **two techniques that operate at opposite ends of the system**.

> **Contextual** is a write-time technique — it improves what gets indexed.
> **Agentic** is a read-time technique — it improves how the index gets used.

Almost nothing about them overlaps, and separating them early avoids a great deal of confusion.

### Contextual retrieval

Solves the fact that **splitting a document destroys context**.

A chunk reading *"The limit is 23 kg."* is perfectly clear in the document, sitting beneath a heading
about Economy checked baggage. Alone in an index it is unfindable — nothing in it mentions baggage,
Economy, or a route, so no question naming those will match it.

The fix: before embedding, a language model reads the chunk alongside some context about its parent
document and writes a short preamble situating it.

```
Original    "The limit is 23 kg."

Preamble    "This passage is from the checked baggage allowance
             table for Economy fares on Abu Dhabi–London routes."

Embedded    preamble + original
Displayed   original only
```

The chunk now matches the question. This is what the `embedding_text` / `display_text` split in the
chunk process exists to support.

The brief's phrasing — *"Embeddings / LLM / Re-ranking"* — names this half's three parts: the
embeddings it improves, the model that writes the preamble, and the re-ranking it pairs with.

### Agentic RAG

Solves the fact that **one search per question is not enough**.

A single-shot pipeline searches once, takes whatever returns, and writes an answer. It has no way to
notice the results are irrelevant, and no way to handle a question that needs two lookups.

Concretely, "agentic" here means the system makes **six decisions** it would otherwise make blindly:

| Decision | The alternative |
|---|---|
| What kind of question is this? | Treat everything identically |
| What is this follow-up actually asking? | Search for *"what about business class?"* literally |
| Is this one question or several? | One search, always |
| Is what I found sufficient? | Assume it is |
| Should I search again, differently? | Never |
| Is my answer actually supported? | Ship it unchecked |

This is what requirements 2.4 and 2.5 ask for.

---

## 2. Use-cases covered

### Contextual — ingestion

| # | Use-case |
|---|---|
| 1 | Generate a situating preamble per chunk |
| 2 | Prepend it to the text that gets embedded |
| 3 | Include it in the keyword index as well as the vector |
| 4 | Cache preambles so unchanged chunks are not regenerated |

### Agentic — query time

| # | Use-case |
|---|---|
| 5 | Classify the question — lookup, comparison, summary, out of scope |
| 6 | Rewrite follow-ups into standalone questions |
| 7 | Decompose multi-part questions into sub-questions |
| 8 | Judge whether retrieved evidence is sufficient |
| 9 | Reformulate and retry when it is not |
| 10 | Verify the drafted answer against retrieved evidence |

---

## 3. Where it fits

### Write path — between chunking and embedding

```
Chunk produced — display_text + heading path
   │
   → generate preamble                                       [#1]  ← one call per chunk
   │
   → embedding_text = heading path + preamble + display_text [#2]
   │
   → hand off for node building, embedding and indexing      [#3]
```

This step **owns the assembly of `embedding_text`**. The preceding stage produces `display_text` and
the heading path but cannot compose the final text, because the preamble does not exist until here.

### Read path — wrapping retrieval

```
Question
   │
   ├─ classify ──────► out of scope? ──► respond, stop     [#5]
   │
   ├─ rewrite (if follow-up)                               [#6]
   │
   ├─ decompose ──► 1..N sub-questions   [N ≤ 4]           [#7]
   │
   │   for each sub-question:
   │      ┌──► retrieve → rerank
   │      │        │
   │      │     sufficient? ──yes──┐                       [#8]
   │      │        │ no            │
   │      └── reformulate          │      [≤ 2 retries]    [#9]
   │           exhausted? ─────────┤
   │                               ▼
   ├─ draft answer from whatever evidence exists
   │
   ├─ verify ──► unsupported claims revised or retracted   [#10]
   │
   └─ respond — with citations, or with "not covered"
```

**Every loop is bounded and every branch terminates.** Four sub-questions maximum, two retries each —
and, because four × three searches is still too many, a **shared budget of roughly six searches per
question** that sub-questions draw from rather than each holding an independent allowance.

The exhausted-retries path must lead to **"the documents don't cover this"** — not to another
attempt, and not to answering anyway from weak evidence. That path is requirement 2.1.

---

## 4. Contextual — detail

### The mechanism

```
Input     context about the parent document + the chunk
Ask       "Write a short passage situating this chunk within the
           document, for search purposes. Reply with only that passage."
Output    1–2 sentences
```

The output constraint matters more than it appears. Left unconstrained, models reply *"Here is the
context you requested: …"* — and that wrapper is embedded along with everything else.

This prompt is subject to requirement 3.3, so it lives in the prompt registry rather than in code. It
is also the prompt most worth iterating on, since it runs across every chunk in the corpus.

### Context is built from structure, not proximity

The technique as published sends **the entire document** with every chunk. That is viable with a
large hosted model and prompt caching; it is not viable here, where a small local model has a modest
context window and every extra token multiplies an already expensive step.

The naive alternative — a fixed character window around the chunk — spends most of its budget on
adjacent prose that says nothing about *where* the chunk sits.

| Source | Cost | What it gives |
|---|---|---|
| Heading path | **Free** — already extracted | Exactly where the chunk sits |
| Document summary | **Once per document** | What the document is about |
| Neighbouring chunk titles | Free | Local surroundings |

Each call therefore carries a document summary generated once, the chunk's heading path, and the
chunk itself. Hundreds of tokens rather than thousands — and at higher information density, because a
heading path states position directly instead of implying it.

### The document summary

**Supplied where the operator supplied one; otherwise generated by a model from assembled
structure.**

An operator-written `description` is authoritative and this stage is skipped entirely — it is more
accurate than anything inferable from two pages, and it cannot hallucinate. What follows describes the
fallback, which is what runs for most documents.

Neither pure option is right for that fallback.

Assembling from headings alone fails at the one job it has: a heading tree reading *"1. Definitions /
2. Scope / 3. General Provisions / 4. Annex C"* says nothing about what the document is. That is
precisely the context-loss problem this technique exists to solve — solving it with a cryptic outline
merely relocates it.

Generating from the whole document may not fit a small model's context, and does not need to.

```
Input     document title — documents.title, or the filename
        + the heading tree
        + first ~2 pages
Ask       "In 2–3 sentences, what is this document and what does it cover?"
Output    a short summary, reused for every chunk in this document
```

**Cost is not an objection here.** This is one call per *document*. Twenty documents means twenty
calls — negligible against the thousands the chunk-level step makes. The economy that constrains
everything else in this technique does not apply, and the instinct to apply it anyway is a scale
error.

⚠️ **The summary's length is paid once per chunk, not once per document.** It is prepended to the
input of every chunk-level call within that document, so a rambling summary multiplies its own cost
by a four-figure factor. This is the only step where a few extra sentences carry that leverage — cap
it.

### Cost of the chunk-level step

One call per chunk. A corpus producing 5,000 chunks means 5,000 generations before anything is
searchable — hours on local hardware, not minutes.

Levers, in order of impact:

1. **A small model for this step specifically** — it is a summarisation task, not reasoning. The
   largest available model is the wrong tool.
2. **Short input** — per the structure-not-proximity approach above.
3. **Capped output** — around 100 tokens. Generation time scales with output length.
4. **Bounded concurrency** — several chunks in flight, limited by what the hardware sustains.
5. **The cache** — makes every run after the first nearly free.

### Caching

Key on **chunk text hash + prompt version + model name**. All three matter: editing the prompt or
changing the model invalidates every preamble even though no document changed. An incomplete key
means serving output from a configuration no longer in use.

**Location:** on disk alongside the parse cache, in `data/preambles/`. Same reasoning as that cache —
regenerable, never searched, only ever fetched by exact key, and mounted from the host so it survives
container restarts. Gitignored.

Keying on **chunk text** rather than position is deliberate and matters more than it appears. Chunk
identifiers derive from position, so retuning chunk size changes every identifier even though no
document changed — but the text of most chunks is unaffected, so their preambles survive. Keying this
cache on position would discard the entire corpus's preambles on every chunking experiment, which is
precisely when they are most expensive to lose.

### The keyword index gets the preamble too

The published technique applies contextualisation to **both** halves of retrieval. Since retrieval is
hybrid, the keyword index is built over the contextualised text as well — otherwise the two halves
search different corpora, and a query matching the preamble is found semantically but not by keyword.

### Guardrails

| Failure | Guard |
|---|---|
| Output wrapped in *"Here is the context…"* | Constrain output in the prompt; strip known lead-ins |
| Preamble states facts absent from the document | Cap length — less room to invent |
| Preamble merely restates the chunk | Adds no retrievability; detect near-duplicates and fall back |
| Generation fails or times out | **Fall back to the heading path alone. Never fail ingestion** |
| Preamble too long | Dilutes the chunk's own vector — the very thing being fixed |

The fallback rule is the important one. A chunk with a mediocre preamble is still indexed and
findable; a chunk that failed ingestion is invisible forever.

---

## 5. Agentic — detail

### Classify

Routes the question: lookup, comparison, summary, or out of scope. Rewriting is not a routing
category — it happens unconditionally within the same planner call. The out-of-scope branch
short-circuits before any retrieval, saving the entire pipeline on greetings and off-topic
questions.

⚠️ **Bias classification toward retrieving.** A wrong "out of scope" refuses a legitimate question,
and the user cannot tell it was a classification error rather than a genuine gap. A wrong "in scope"
costs one wasted search. The costs are not symmetric — when uncertain, retrieve.

### Rewrite follow-ups

*"What about business class?"* becomes *"What is the checked baggage allowance for Business class on
Abu Dhabi–London?"*

This **must** happen before retrieval, which sees only a query string and has no access to
conversation history. If rewriting does not happen here, it does not happen at all.

The step should resolve references, not add specificity nobody asked for.

### Decompose

*"How does A compare to B?"* becomes two sub-questions and a synthesis.

Two constraints, both about cost: **cap the number of sub-questions** at four, and **decompose
conservatively**. A simple lookup split into three sub-questions triples the work and degrades the
answer.

### Judge sufficiency

Two signals are available, at very different costs:

| Signal | Cost | Reliability |
|---|---|---|
| Re-rank score | Free — already computed | Decisive at the extremes |
| Model judgement | An extra call | Better in the ambiguous middle |

**Use the free signal first.** If the top score is clearly high, evidence is sufficient — no call. If
clearly low, insufficient — no call. Only the ambiguous band invokes the judge. That converts an
every-question cost into a sometimes cost.

#### Thresholds

Three thresholds, doing three different jobs — they are conflated constantly:

| Threshold | Applies to | Decides |
|---|---|---|
| `keep_floor` | Each chunk individually | Whether this chunk enters the answer context at all |
| `sufficient_high` | Top-scoring chunk | Evidence is clearly good → skip the judge |
| `insufficient_low` | Top-scoring chunk | Evidence is clearly bad → skip the judge, go to retry |

**Sufficiency keys on the top-1 score, not the average.** One strongly relevant passage answers a
question; five mediocre ones usually do not. Averaging lets a cluster of near-misses masquerade as
evidence.

Cross-encoder outputs are raw logits, effectively unbounded and not comparable to anything. Pass them
through a sigmoid to land on 0–1 before thresholding, so the numbers in configuration mean something
to whoever reads them later.

**These numbers cannot be chosen by reasoning.** The decision recorded here is the calibration
procedure:

1. Build a small labelled set — questions with known-relevant chunks, **plus questions the corpus
   genuinely cannot answer**
2. Run retrieval and re-ranking over both, collecting top-1 scores
3. Plot the two distributions
4. Set `sufficient_high` where relevant questions cluster and `insufficient_low` where unanswerable
   ones do
5. The overlap between them *is* the ambiguous band, which sizes how often the judge fires

If the distributions do not separate, that is itself information: the re-ranker is not
discriminating, and no threshold will rescue it.

**Provisional starting values:** `keep_floor 0.3` · `insufficient_low 0.3` · `sufficient_high 0.7`.

⚠️ Explicitly arbitrary — placeholders so the pipeline runs end to end before calibration data
exists. Recorded as provisional, not as chosen.

One thing to watch during calibration: different question types have different natural score ranges —
a specific lookup peaks higher than a broad summary request. If that appears strongly, thresholds may
need to vary by the classification from use-case 5. Worth checking for; not worth building until
seen.

### Reformulate and retry

When evidence is insufficient, generate a *different* query — not the same one again. Strategies:
different vocabulary, broaden the scope, or draw terms from the weak results to search in the
document's own language.

**Track what has been tried.** Without that, a model will reformulate to something near-identical and
burn the retry budget going nowhere.

### Verify

Each claim in the draft is checked against retrieved evidence, then kept, revised, or retracted.

One subtlety: **the verifier should see the evidence and the answer, not the reasoning that produced
them.** Given the chain of thought that justified a claim, a model tends to accept the claim. Given
only the passages, it checks.

### Failure modes

| Failure | Guard |
|---|---|
| Retry loop never terminates | Hard cap, and reformulations must differ from what has been tried |
| Decomposition explodes | Cap sub-questions; decompose only when genuinely multi-part |
| Judge always says "insufficient" | System refuses everything — calibrate against known-answerable questions |
| Judge always says "sufficient" | The loop never fires; this is a single-shot pipeline with extra steps |
| Verifier retracts everything | Answers become uselessly hedged |
| A stage errors | **Fail forward** — degrade to the simpler path rather than failing the request |

The two middle rows deserve the most attention: both leave the system *looking* like it works while
the agentic behaviour does nothing at all.

### Latency

Every stage is a model call. Classification, rewriting and decomposition are merged into a single
planner call, and sufficiency judging is free at the score extremes — so a simple lookup costs
**four calls**, a two-part comparison with one retry each about seven, and an out-of-scope question
one. On local hardware that is roughly 20–40 seconds depending on profile.

This is not a problem to defer; it is a constraint that shapes the interface. A user watching a blank
screen for a minute assumes the system is broken.

---

## 6. Proving it worked

Ablation, for both halves — *"we implemented it"* is not evidence it helped.

| Turn off | Expect to drop |
|---|---|
| Contextual preambles | Retrieval recall |
| Retry loop | Context recall |
| Verifier | Faithfulness |
| Decomposition | Correctness on multi-part questions only |
| Re-ranker | Context precision |
| Fast path | Nothing, ideally — that is what makes it defensible |

⚠️ **One test set that is easy to forget: questions the corpus genuinely cannot answer.** Without
them, whether the system correctly declines is never measured — half of requirement 2.1, and
invisible in ordinary testing because every question one naturally thinks to ask has an answer.

---

## 7. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Contextual context source | Document summary + heading path | Not a raw text window — structure carries more per token |
| Document summary | Operator description where supplied; otherwise model-generated from title, heading tree and first ~2 pages | Headings alone are too cryptic to situate anything |
| Document summary frequency | Once per document | Cost objections here are a scale error |
| Document summary length | Capped | Paid once per *chunk*, not once per document |
| Contextualisation model | Small, distinct from the answering model | A summarisation task, not reasoning. Which model → Ollama, design choice 5.5 |
| Preamble length | Capped ~100 tokens | Generation time scales with output; longer dilutes the vector |
| Preamble cache key | Chunk **text** hash + prompt version + model name | Any of the three changing invalidates the preamble. Keyed on text, not position, so chunking experiments do not discard the corpus's preambles |
| Preamble cache location | Disk — `data/preambles/` | Regenerable, never searched, fetched only by exact key |
| `embedding_text` assembly | Owned by this step | The preceding stage cannot compose it — the preamble does not exist until here |
| Preamble generation failure | Fall back to heading path | Never fail ingestion — an unindexed chunk is invisible forever |
| Keyword index content | Contextualised text | Otherwise the two halves of hybrid retrieval search different corpora |
| Classification bias | Toward retrieving when uncertain | Error costs are asymmetric |
| Follow-up rewriting | Before retrieval | Retrieval has no access to conversation history |
| Sufficiency signal | Re-rank score first, model judgement only in the ambiguous band | Converts an every-question cost into a sometimes cost |
| Sufficiency basis | Top-1 score, not the average | Averaging lets near-misses masquerade as evidence |
| Score normalisation | Sigmoid to 0–1 before thresholding | Raw logits are not comparable to anything |
| Threshold selection | By calibration procedure, not by reasoning | Recorded values are provisional placeholders |
| Sub-question cap | 4 | |
| Retry cap | 2 per sub-question, within a shared budget of ~6 searches per question | Reformulations must differ from prior attempts |
| Exhausted retries | Answer "not covered" | Never answer from weak evidence — requirement 2.1 |
| Verifier input | Evidence + answer only, not the reasoning | Shown the reasoning, a model accepts the claim |
| Stage failure | Fail forward to the simpler path | Degrade rather than fail the request |

### Still open

| Item | Decided under | Settled by |
|---|---|---|
| Which model performs contextualisation | Ollama — design choice 5.5 | Evaluation |
| Exact prompt wording | Arize Phoenix — design choice 5.8 | Evaluation |
| Final threshold values | This document | The calibration procedure in §5 |
| Whether thresholds vary by question type | This document | Calibration, if the distributions warrant it |
