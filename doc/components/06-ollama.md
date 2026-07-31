# Ollama

> Baseline item 6 of 10 — *"Ollama — Model hosting"*.
>
> **This document is the authority on model selection.** Other components state what they need from a
> model; which model provides it is decided here. Design choice 5.5.
>
> Serves constraint 3.1 (entirely open source) and supplies every other component that calls a
> language model.

---

## 1. What it is

Software that runs language models on local hardware — it downloads weights and serves them over an
HTTP API.

It is what makes constraint 3.1 achievable: no commercial API, models running on machines we control.

**Scope boundary:** Ollama hosts *language* models. The re-ranker is a cross-encoder, a different
serving pattern, and runs in-process rather than through Ollama. Item 6 does not cover every model in
the system.

---

## 2. How many models

Not one. Four distinct jobs:

| Job | Called | Needs |
|---|---|---|
| **Embedding** | Every chunk at ingest; every query at read | Fast, strong retrieval quality. Fixes `N` in the storage schema |
| **Contextualisation** | Once per chunk — thousands of calls | Small and fast above all. Summarisation, not reasoning |
| **Answering** | 2–4 times per question | Instruction-following and reliable structured output |
| **Summarisation** | Once per document; after each response | Same profile as contextualisation |
| **Verification** | Once per question | A checking task, not a reasoning one — see §5 |
| **Judging** | Evaluation only, offline | A *different family*, to avoid grading its own homework |

Contextualisation, summarisation and verification share requirements, so serving needs **three
models**. Evaluation adds a fourth which is **never resident at the same time** — it is a CLI, run
while serving is idle, so it does not compete for memory.

---

## 3. Where each fits

```
INGESTION                          SERVING
  parse                              classify      ─┐
  summarise      ← small model       rewrite        ├─ answering model
  chunk            (per DOCUMENT)    decompose      │
  contextualise  ← small model       draft         ─┘
  embed          ← embed model       judge          ── only if ambiguous
  index                              verify         ── small model
                                     embed query   ← embed model
                                     rerank        ← cross-encoder, not Ollama
```

The two phases use **different pairs**. Ingestion needs embedding + small; serving needs embedding +
answering. They do not overlap in normal operation, which is what keeps memory manageable.

---

## 4. Model selection

> ⚠️ **REVISED AFTER MEASUREMENT.** The original selection below the line was
> written before anything ran. It assigned **reasoning** models (`qwen3:*`) to
> per-request work on the argument that reasoning helps planning and grounding,
> and that call volume was low enough to afford it. Measured, that was wrong
> three times over. The table here is what the system actually runs; §4.1 records
> what changed and why.

| Job | Model | Approx. size | Why |
|---|---|---|---|
| Embedding | **bge-m3** | ~1.2 GB | 1024-dim, 8k context, strong retrieval benchmarks, multilingual as a hedge against unknown client documents |
| Contextualisation | **qwen2.5:3b** | ~1.9 GB | Highest-volume call in the system — one per chunk. Must not reason |
| Document summarisation | **qwen3:4b** | ~2.5 GB | Ingestion only, once per document, off any critical path — reasoning is affordable here |
| Planning, retrieval, synthesis | **qwen2.5:7b** | ~4.7 GB | Structured-output reliability without chain-of-thought overhead |
| Verification | **qwen2.5:7b** | *(resident)* | Same model, so verification costs no model switch |

`bge-m3` sets `N = 1024` in the storage schema.

### 4.1 ⚠️ Why the reasoning models were removed

The same mistake was made three times, each time justified by *call volume* when
the axis that mattered was *per-call cost*:

| Stage | With a reasoning model | Consequence |
|---|---|---|
| Contextualisation | ~5,000 tokens and ~194 s to write **one** situating sentence | ~32 hours for a 589-chunk corpus |
| Planning + synthesis | ~79 s per call | **553 s** for a single answer |
| Verification | 177 s in one call | **76%** of a 233 s answer |

Verification is the instructive one, because speed was not allowed to decide it.
Each candidate was given a draft containing an invented figure — "the fee is
exactly USD 150" — over passages that mention no number:

| Model | Time | Caught the fabrication? |
|---|---|---|
| `qwen3:4b` | 100.6 s | ✅ |
| **`qwen2.5:7b`** | **8.8 s** | ✅ — byte-identical output |
| `qwen2.5:3b` | 4.6 s | ❌ **passed "$150" through** |

The fastest candidate is disqualified: verification is the last guard against a
fabricated claim reaching a reader. `qwen2.5:7b` gives identical verification 11×
quicker and is already resident.

**`small`, `cheap` and `adequate` are three different axes**, and this design
collapsed them into one until each was measured separately. A unit test now fails
if any read-path model comes from a reasoning family.

---

<details><summary>Original pre-implementation selection (superseded)</summary>

| Job | Model | Why |
|---|---|---|
| Contextualisation + summarisation | qwen3:4b | Small enough to run thousands of times |
| Answering | qwen3:8b | Best structured-output reliability at a size that fits |

The two generative models share a family — consistent prompt handling, one fewer
variable when debugging. That reasoning still holds; the family chosen was wrong.

</details>

**`bge-m3` sets `N = 1024`** in the storage schema.

⚠️ Sizes and tags are indicative and must be verified against the registry at implementation.

### Memory arithmetic

16 GB total, shared with the database, tracing, the interface, and the backend — roughly 5–6 GB
consumed before Ollama starts.

| Phase | Resident | Total |
|---|---|---|
| Ingestion | bge-m3 + qwen3:4b | ~3.7 GB |
| Serving | bge-m3 + qwen3:8b + qwen3:4b | ~8.7 GB |
| Evaluation *(offline)* | bge-m3 + judge model | ~6 GB |

Serving fits in the ~11 GB available, with less headroom than ideal and **no swapping**.

**Ingestion's models are a subset of serving's**, so running ingestion while serving costs no extra
memory and triggers no loading — the two contend for compute only. Evaluation never overlaps with
serving, being a development activity.

---

## 5. Model loading and swapping

### How it works

A model loads into memory on first request, stays resident for `keep_alive` (default 5 minutes), then
unloads. Loading a 5 GB model means reading it from disk — seconds, not milliseconds.

Fine occasionally. Catastrophic per chunk.

### ⚠️ The interleaving disaster

The natural way to write ingestion is per chunk:

```
for chunk in chunks:
    preamble = context_model(chunk)     # qwen3:4b
    vector   = embed_model(chunk)       # bge-m3
```

If memory pressure or configuration keeps only one model resident, **every chunk triggers two model
loads**:

```
5,000 chunks × 2 loads × ~5s  =  ~14 hours
```

Of loading. The inference is incidental.

It degrades silently — the pipeline works, produces correct output, and is inexplicably slow. Nothing
reports that 95% of the time is spent loading models.

### The rule: batch by model, not by chunk

```
Phase 1   summarise ALL documents      qwen3:4b loaded once
Phase 2   contextualise ALL chunks     qwen3:4b still loaded
Phase 3   embed ALL chunks             bge-m3 loaded once
Phase 4   insert
```

Two model loads for the entire corpus, regardless of size. Phases 1 and 2 share a model, and phase 1
must precede phase 2 — each document's summary is an input to every one of its chunk-level calls.

**The preamble cache makes this natural rather than awkward.** The obvious objection to two passes is
holding everything in memory between them — but phase 1 already writes its output to
`data/preambles/`. Phase 2 is an independent pass reading chunks and looking up their preambles.
Neither phase needs the other in memory, and either can be resumed after a crash.

A cache introduced to avoid recomputation is what makes the phase separation practical.

### Configuration per phase

| Setting | Ingestion | Serving |
|---|---|---|
| `keep_alive` | Long — models in constant use | Long for both resident models |
| `OLLAMA_MAX_LOADED_MODELS` | ≥ 2 | ≥ 2 |
| `OLLAMA_NUM_PARALLEL` | 2–4 | 1–2 |

**`keep_alive` during serving matters more than it appears.** The five-minute default means an idle
system unloads the answering model, and the next question pays a cold start on top of an already
slow response — which, during a demonstration, is the first question anyone asks. A warmup call at
startup handles the first request; a long `keep_alive` handles idleness. Both are cheap.

**Parallelism costs memory.** Each concurrent slot holds its own key-value cache. It helps ingestion
throughput, where independent chunks queue up. It does nothing for a single question, because the
reasoning stages are sequential by design.

### The consequence for serving

**The verifier runs on qwen3:4b, and all three models stay resident.**

Verification dominated the latency budget at ~22s on the answering model. It is a *checking* task —
closer in kind to contextualisation than to drafting — so the small model is the right tool, not
merely the cheaper one.

| | Two generative models resident |
|---|---|
| bge-m3 + qwen3:8b + qwen3:4b | **8.7 GB** of ~11 GB available |
| Saving | **~11s on every question** |
| Loading in the request path | **None** — both resident |

The alternative, keeping the small model out of serving, would have cost that 11 seconds on every
question to buy headroom that is not needed. Conversation summarisation also runs at serving time on
the small model, which the ingestion-only split would have made impossible.

---

## 6. Context window

### ⚠️ The silent truncation

Ollama sets a **runtime** context window far smaller than most models support — commonly 4096 tokens,
sometimes 2048. Exceed it and the prompt is truncated, with no error and no warning. The model then
answers from whatever survived.

This is the same failure the context-budget work identified, arriving from the serving layer rather
than our own accounting — and worse, because our accounting can be perfect and this still happens.

### What is actually needed

Computed rather than guessed, for the largest stage:

| Component | Tokens |
|---|---|
| Instructions — agent prompt plus citation rules | ~700 |
| Evidence — 5 chunks × 768 *(quality profile)* | ~3,840 |
| Memory — summary plus recent turns | ~1,300 |
| Question | ~50 |
| Generation headroom | ~1,000 |
| | **≈ 6,900** |

| Model | `num_ctx` | Reasoning |
|---|---|---|
| qwen3:8b — answering | **8192** | The drafting stage above, with headroom |
| qwen3:4b — contextualisation, summarisation, verification | **8192** | Contextualisation needs ~1,200; verification sees the full evidence set and so needs the same window as drafting |
| bge-m3 — embedding | 8192 native | Chunks are ~900 tokens; no configuration needed |

### A decision this forces

Working out the evidence figure exposed an unresolved ambiguity: **which text goes to the drafting
model — `display_text` or `embedding_text`?**

**The preamble is synthetic text we generated.** If the drafting model sees it, the model can quote
it — and a quoted phrase appearing nowhere in the document then arrives under a page citation. That
is precisely the fabrication the citation design exists to prevent, reintroduced through the answer
path instead of the display path.

**`display_text` goes to the drafting model.** The heading path travels alongside as metadata,
clearly separated from the passage. The preamble's job is retrieval; it has no place in generation.

This also confirms the evidence budget as `768 × 5 = 3,840` — the arithmetic holds because the
preamble never enters the answer context.

### The memory cost of context

The key-value cache scales with `num_ctx` and with `NUM_PARALLEL`.

| `num_ctx` | Rough KV cache, 8B model |
|---|---|
| 4096 | ~0.3 GB |
| **8192** | **~0.6 GB** |
| 32768 | ~2.4 GB |

8192 costs a few hundred megabytes — affordable. 32768 would not be, and is unnecessary since the
context budget caps what we send well below 8192.

⚠️ Note the multiplier: `NUM_PARALLEL 4` at 8192 means four such caches. Ingestion parallelism and
this setting interact.

---

## 7. Quantization

| | 8B model | Quality |
|---|---|---|
| fp16 | ~16 GB | Reference |
| Q8_0 | ~8.5 GB | Essentially indistinguishable |
| Q5_K_M | ~5.7 GB | Very close |
| **Q4_K_M** | **~4.9 GB** | Small, well-characterised loss |
| Q3 / Q2 | smaller | Noticeable degradation |

**Q4_K_M for both generative models.** The only tier leaving room at 16 GB, and the quality loss sits
well below the variation caused by prompt wording.

### ⚠️ Not the embedding model

Different situation, for two reasons.

**It is already small.** At ~1.2 GB it is not what strains the budget, so there is nothing to buy.

**Its output quality is the foundation.** A quantized generative model writes slightly worse prose. A
quantized embedding model produces slightly worse vectors — degrading retrieval, and therefore
everything downstream, with no symptom. Just marginally worse results, permanently.

**Changing the embedding model's quantization changes its embeddings**, so it carries the same
re-embedding cost as changing the model itself. A versioned decision, not a tuning knob.

---

## 8. Determinism

### Why it matters

**Evaluation.** If identical input produces different output, a metric moving cannot be attributed to
a change rather than to sampling. Every ablation depends on runs being comparable.

**Debugging.** A failure that cannot be reproduced cannot be fixed.

**Consistency as a product property.** Not merely an engineering convenience. Two colleagues asking
the same question about an allowance should receive the same answer. A policy-lookup tool that
responds differently each time is one nobody trusts — rightly, since the variation carries no
information.

### Temperature by stage

| Stage | Temperature |
|---|---|
| Classify | **0** |
| Rewrite follow-up | **0** |
| Decompose | **0** |
| Judge sufficiency | **0** |
| Draft answer | **0** |
| Verify | **0** |
| Contextualise | **0** |
| Summarise | **0** |

**Temperature 0 everywhere.** Only drafting is genuinely arguable: a little temperature avoids
repetitive phrasing, but the answer should be determined by retrieved evidence, not by sampling. Any
variation between two runs on identical evidence is by definition not coming from the documents —
which is the one place answers are supposed to originate.

On contextualisation: at temperature 0 a preamble is a pure function of its inputs. Above 0, the
cache silently freezes whichever output happened to be produced first, making the corpus's preambles
dependent on processing order. Determinism makes the cache honest.

### ⚠️ The honest caveat

**Temperature 0 does not guarantee identical output.** Greedy decoding is deterministic given
identical numerics — but floating-point addition is not associative, and batching, parallelism and
scheduling can change operation order. Setting a `seed` helps without closing it.

Consequences:

- **Evaluation must not rest on exact-match output comparison** — it produces phantom regressions
- **Enough examples that noise averages out.** A metric over 50 questions tolerates occasional
  variation; over 5 it does not
- **A bug that will not reproduce may genuinely be this**, not a mistake in the investigation

### Structured output

Four stages need parseable output rather than prose: classification, decomposition, sufficiency,
verification.

Prompting an 8B model for JSON and parsing works until it does not — models drift out of format
mid-answer, wrap output in prose, or append commentary.

**Use enforced structured output where the serving layer supports it.** A schema constrains generation
itself rather than asking politely. That removes a class of parse failures rather than handling them.

Where unavailable or failing anyway: bounded retry, then **fail forward** to the simpler path. An
unparseable classification defaults to *retrieve* — the safe branch, since the error costs are
asymmetric.

---

## 9. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Scope | Language models only | The cross-encoder re-ranker runs in-process |
| Number of models | **Three at serving**, plus an offline judge | Contextualisation, summarisation and verification share a profile. The judge is never resident concurrently |
| Embedding model | **bge-m3** | Sets `N = 1024` in the schema |
| Contextualisation, summarisation, verification | **qwen3:4b** | Thousands of calls at ingest; the latency lever at serving |
| Answering model | **qwen3:8b** | Structured-output reliability at a size that fits |
| Model family | Shared between generative models | One fewer variable when debugging |
| Ingestion structure | **Batch by model, not by chunk** | Interleaving costs hours of pure loading, silently |
| Phase separation | Contextualise all → embed all → insert | Made practical by the preamble cache; each phase resumable |
| `keep_alive` | Long in both phases | The default means the first question after idle pays a cold start |
| Warmup | Load serving models at startup | Removes the first-request penalty |
| `OLLAMA_MAX_LOADED_MODELS` | ≥ 2 | Below this, eviction reintroduces per-call loading |
| `OLLAMA_NUM_PARALLEL` | 2–4 ingestion, 1–2 serving | Each slot costs a KV cache; sequential stages gain nothing |
| **Verifier model** | **qwen3:4b** | A checking task, not a reasoning one. Saves ~11s per question; all three models stay resident |
| `num_ctx` | Set explicitly per model | The default truncates prompts silently |
| Answering window | 8192 | Computed from the drafting stage |
| Small-model window | 8192 | Contextualisation needs ~1,200, but verification sees the full evidence set |
| **Text sent to the drafting model** | **`display_text` only** | The preamble is synthetic; quoting it would place invented wording under a page citation |
| Heading path in answer context | Passed as metadata, separate from the passage | |
| Generative quantization | Q4_K_M | The only tier leaving room at 16 GB |
| Embedding quantization | **None beyond default** | Degraded vectors harm retrieval invisibly |
| Embedding quantization changes | Treated as a model change | Requires re-embedding |
| Temperature | **0, every stage** | Including drafting — consistency is a product property here |
| `seed` | Set explicitly | Helps reproducibility without guaranteeing it |
| `top_p` / `top_k` | Set explicitly, not inherited | Model defaults vary |
| Determinism expectation | **Near, not absolute** | Floating-point and batching effects persist at temperature 0 |
| Evaluation design | No exact-match comparison; enough examples to average noise | |
| Decision-stage output | Enforced structured output where supported | Prompting for JSON is unreliable at 8B |
| Parse failure | Bounded retry, then fail forward | Unparseable classification defaults to *retrieve* |

### Still open

| Item | Settled by |
|---|---|
| Exact model tags and sizes | Verification against the registry at implementation |
| Which family judges during evaluation | Availability — the budget is not shared with serving |
| Whether qwen3:8b's structured-output reliability is sufficient in practice | Measurement during the first working pipeline |
| `NUM_PARALLEL` for ingestion, against actual memory headroom | Measurement |
