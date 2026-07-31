# PGVector / PostgreSQL

> Baseline item 2 of 10 — *"LlamaIndex + PGVector/PostgreSQL — RAG methodology"*.
>
> The baseline bundles a framework and a database into one item. This document covers the database;
> the framework is covered in [02 · LlamaIndex](02-llamaindex.md).
>
> **This document is the authority on the storage schema.** Other components describe their own
> contributions to it; the complete definition lives here.
>
> Covers requirement 2.6 (indexing) and serves 2.2, 2.3, 3.2.

---

## 1. What it is

Two things bundled: **PostgreSQL**, a relational database, and **pgvector**, an extension adding a
vector column type and similarity search over it.

Its scope here is broader than any other component:

> **The single store for everything that persists** — chunks, vectors, the keyword index, document
> records, conversation memory, and ingestion history.

One system rather than several. That is the decision worth defending, and it pays off in three
specific places:

- **Transactions.** Delete-then-insert on re-ingestion must be atomic. Across two systems it cannot
  be.
- **Filtered vector search in one query.** *"Nearest chunks, but only from these documents"* is a
  single statement rather than a search followed by filtering in application code.
- **One thing to operate.** No second service to deploy, back up, or keep synchronised.

---

## 2. Use-cases covered

| # | Use-case | Purpose |
|---|---|---|
| 1 | Store chunk records | The complete schema |
| 2 | Vector similarity search | The semantic half of retrieval |
| 3 | Full-text search | The keyword half |
| 4 | Metadata filtering | Restrict by document, collection, or any `extra` field |
| 5 | Store document records | Title, hash, operator-supplied metadata, and the once-per-document summary |
| 6 | Store conversation memory | Turns and rolling summaries |
| 7 | Atomic re-ingestion | Delete and insert in one transaction |
| 8 | Isolate collections | Several document sets in one deployment. The collection is **assigned at ingestion** — a CLI flag or upload field, defaulting to `default` — and queries filter to the configured active collection |
| 9 | Record ingestion runs | Which configuration produced the current index |

---

## 3. Schema

```
ingestion_runs                        documents
  id                PK                  id                  PK
  collection                            collection
  started_at                            source_file         ── unique with collection
  finished_at                           doc_hash
  status                                title               ── supplied, else extracted
  documents_seen                        description         ── supplied; overrides summary
  documents_parsed                      summary             ── generated fallback
  documents_skipped                     effective_date
  chunks_created                        confidentiality     ── public | internal | confidential
  preambles_generated                   extra               jsonb
  config          jsonb                 ingestion_run_id    FK → ingestion_runs
  error                                 ingested_at

conversations                         chunks
  id              PK                    id              PK  ── deterministic chunk_id
  summary         jsonb                 doc_id          FK → documents  ON DELETE CASCADE
  created_at                            collection
  last_active_at                        display_text
                                        embedding_text
messages                                embedding       vector(N)
  id              PK                    tsv             tsvector
  conversation_id FK → conversations    page
  turn_index                            position
  role                                  extra           jsonb
  content
  rewritten_query
  citations       jsonb
  prompt_versions jsonb   ── which prompt version produced this answer
  provenance          ── cited / hedged / declined
  trace_id            ── the join key into the tracing system
  latency_ms
  tokens_in
  tokens_out
  created_at
```

### Operator-supplied fields on `documents`

Five of the columns on `documents` may be supplied at upload rather than derived. The governing
principle, argued in [11 · FastAPI](11-fastapi.md) §4:

> **Operator-supplied metadata is authoritative. Generated metadata is the fallback.**

| Column | Supplied | Absent |
|---|---|---|
| `title` | Used | Extracted at parse, else the filename |
| `description` | Becomes the document summary, and feeds the corpus description | `summary` is generated and used in its place |
| `effective_date` | Recorded | Null |
| `confidentiality` | Applied | Defaults to `internal` |
| `extra` | Merged | Empty |

`description` and `summary` are **two columns, not one**, so it is always possible to tell which is
being read — and regenerating a summary can never overwrite what a human wrote.

`confidentiality` gates whether the source PDF may be served and deep-linked at `#page=14`. That
question previously had no owner; the document now carries its own answer.

### Why the schema separates fixed columns from `extra`

The structure of an arbitrary PDF cannot be known in advance. Fixed columns hold what is always true
and gets filtered or sorted on; `extra` holds everything that varies by document — heading path,
document title, content type, token count.

The discipline this requires: **resist adding a column each time a new field appears.** That is how
the schema stops working for the next document set.

### The rule that governs where things live

> **Chunks carry chunk-level facts only.** Anything true of the whole document — filename, hash,
> title, summary — lives on `documents` and is reached by join. Anything derivable from stored
> columns is derived, not stored.

Three things follow from it, each of which was got wrong somewhere before being corrected:
`source_file` and `doc_hash` belong on `documents`, not repeated on every chunk; the document title
likewise; and neighbour links need no columns at all, since `(doc_id, position)` already identifies
them.

### `ON DELETE CASCADE` implements the re-ingestion rule

Re-ingesting a changed document must remove the old version's chunks before inserting new ones —
otherwise two versions coexist in the index and a superseded clause can be cited as current.

With the cascade, that rule becomes: delete the document row, chunks vanish with it, insert the new
version. One statement, naturally atomic, and incapable of leaving orphans.

### Two texts per chunk

`display_text` is the chunk exactly as it appears in the document — what citations quote.
`embedding_text` is the heading path, contextual preamble and original combined — what becomes the
vector and what the keyword index is built over.

They are separate columns and must never be conflated. Rendering `embedding_text` under a page
citation would display words that do not appear on that page.

---

## 4. Operational data

### The split

> **The tracing system owns telemetry. This database owns durable operational state.**

Per-call latency, token counts, inputs and outputs are already captured by tracing (requirement
3.2). Copying them here duplicates a system already mandated.

The test for what belongs here: **would losing it break something, or does the application itself
query it?** Telemetry serves humans debugging; operational state serves the system operating.

| | Database | Tracing |
|---|---|---|
| Which configuration produced the current index | ✓ | |
| Conversation lifecycle and retention | ✓ | |
| Whether a turn was answered or declined | ✓ | |
| Latency per stage | | ✓ |
| Tokens per model call | | ✓ |
| The span tree of a request | | ✓ |

### Ingestion runs

About reproducibility rather than metrics. When retrieval quality changes, the first question is
*what changed* — chunk size, embedding model, preamble prompt? Without a run record that is
archaeology; with one it is a lookup, and `config` captures the exact settings so two runs can be
compared directly.

The `documents_parsed` versus `documents_skipped` and `preambles_generated` counts also make the
caches observable. A run regenerating every preamble when nothing changed means a cache key is wrong,
and nothing else would surface that.

### The join key

`messages.trace_id` connects the two systems: from a stored conversation turn, jump straight to the
trace that produced it. Without it, correlating *"this answer was wrong"* with *"here is what
happened"* means matching on timestamps.

`latency_ms`, `tokens_in` and `tokens_out` are stored despite being available in tracing, so that
basic questions — average response time, tokens per conversation — do not require the tracing system
to be running and unpurged.

### Retention

⚠️ Run records and conversations grow without bound. `conversations.last_active_at` is what a
retention policy acts on, and a scheduled task exists to execute one — see
[11 · FastAPI](11-fastapi.md) §5. **The periods themselves are still undecided**, and that is a
policy question with a privacy dimension rather than a technical one. A position is needed before the
tables fill, not after.

---

## 5. Vector index

### The choice: HNSW

| | Exact (no index) | IVFFlat | HNSW |
|---|---|---|---|
| Recall | **100%** | Approximate | Approximate, better |
| Build | None | Fast | Slower |
| Memory | None | Low | Higher |
| Needs training data | No | **Yes** | No |
| Handles inserts | N/A | Degrades | Gracefully |

Two reasons, the second decisive.

**Recall matters more than latency here.** The two-stage design casts wide and lets the cross-encoder
do the precision work — but the re-ranker can only rescore what retrieval handed it. A candidate the
index missed is unrecoverable, and invisible: five plausible passages arrive with no indication a
better sixth existed.

**IVFFlat fits the ingestion pattern badly.** It clusters vectors and must be built *after* data is
loaded, because it needs data to cluster on. Ingestion here is incremental, and re-ingestion is
delete-then-insert per document, so those clusters drift as content changes and require periodic
rebuilds. That is a recurring operational chore with a silent failure mode.

HNSW accepts inserts without degrading and needs no training pass.

### On using no index at all

Genuinely viable at this scale — exact search over a few thousand vectors is milliseconds and gives
perfect recall. But it does not scale, and migrating later is more disruptive than building the index
now, when it costs seconds. HNSW is what would be deployed, so it is deployed.

Exact search remains useful as a measuring tool — see below.

### Parameters

| Parameter | Value | Note |
|---|---|---|
| `m` | 16 | Default. Connections per node |
| `ef_construction` | 64 | Default. Higher builds a better index, more slowly |
| `ef_search` | **~100** | Not the default of 40 |

`ef_search` bounds how many candidates the search explores and must exceed the `k` being retrieved —
meaningfully, not marginally. Retrieving 20 with the default of 40 leaves little headroom and quietly
costs recall. It is a query-time setting, tunable without rebuilding.

### ⚠️ The silent gotcha

**The index is used only if the query operator matches the operator class it was built with.**

Built with `vector_cosine_ops`, queried with `<=>`. A different distance operator produces a
sequential scan — correct results, no error, and performance that collapses quietly as the corpus
grows.

Nothing warns about this. Running `EXPLAIN` on the retrieval query once is what catches it.

Cosine is correct given normalised embeddings, and is stated explicitly rather than left to a client
library's default.

### Filtered search is the weak spot

Retrieval filters by collection, document, and `extra` fields. **An approximate index combined with a
`WHERE` clause is where these struggle**: the index returns its best *k* by distance, the filter then
removes some, and fewer results come back than were asked for. Not wrong — short, and silently.

Recent pgvector versions mitigate this with iterative scanning; the version pinned must have it. If
filtering proves heavy, the alternatives are partial indexes per collection, or over-fetching and
filtering in application code.

### Measure the recall

Approximate means approximate, and nothing reports what was lost.

**Run the same queries with and without the index** — exact search as ground truth, HNSW as the
candidate — and compare the returned sets. That produces an actual recall figure for the chosen
`ef_search`, on this corpus, rather than a parameter taken from a blog post. It is cheap at this
size, and worth repeating as the corpus grows, since that is when approximate indexes quietly get
worse.

---

## 6. Hybrid search mechanics

### The fusion problem

The two searches return incomparable numbers.

| | Range | Depends on |
|---|---|---|
| Cosine distance | 0–2, bounded | The vectors |
| `ts_rank` | Unbounded | Term frequency, document length, query length |

`0.6 × cosine + 0.4 × ts_rank` is meaningless arithmetic: different scales, different distributions,
and `ts_rank` shifts with the query itself, so one weighting behaves differently on a three-word
question than a fifteen-word one.

### The reframe that settles it

**Fusion feeds the re-ranker.** Its job is getting the right candidates *into the set*, not ordering
them well — the cross-encoder rescores everything downstream regardless.

That removes any need for precise magnitude, and with it the reason to normalise scores at all.

### Reciprocal Rank Fusion

RRF combines **ranks**, not scores:

```
score(chunk) = Σ  1 / (k + rank_in_list_i)        k ≈ 60
```

A chunk ranked 1st by vector search and 8th by keyword search scores `1/61 + 1/68`. Appearing in both
lists beats ranking well in one — the desired behaviour, since agreement between two different
retrieval methods is a strong signal.

| | RRF | Normalise + weight |
|---|---|---|
| Scale-free | ✓ | Requires calibration |
| Stable across queries | ✓ | Min-max shifts with each result set |
| Weights to tune | None needed | Two, per corpus |
| Preserves magnitude | ✗ | ✓ |

The one column RRF loses is the one that does not matter here.

### Fusion runs in the application

Both are possible — SQL can express RRF with CTEs in a single round trip. Application-side wins on
three counts:

- **Traceability.** Requirement 3.2 wants retrieval visible. Two queries produce two spans, showing
  what each half found. One fused statement is a single opaque span.
- **Debuggability.** When retrieval goes wrong the first question is *which half failed*. Separate
  queries answer it immediately.
- **Flexibility.** Changing the fusion method becomes a code change rather than a SQL rewrite.

The cost is two round trips to a local database. Negligible.

⚠️ Filters must be applied **identically to both queries**. Easy to get right, easy to forget — and
if they drift, the two halves are searching different corpora.

### The keyword side

**Language configuration.** `english` applies stemming and stop-words, so *"cancelled"* matches
*"cancellation"*. `simple` does neither. English suits prose-heavy policy documents; it is a
per-column setting and multilingual corpora would want it revisited.

**⚠️ Query construction — the trap.** The obvious choice, `plainto_tsquery`, joins every term with
**AND**. For a natural-language question that is fatal:

```
"What is the checked baggage allowance for Economy passengers
 travelling from Abu Dhabi to London?"

→ what AND checked AND baggage AND allowance AND economy
  AND passengers AND travelling AND abu AND dhabi AND london

→ zero results
```

No error, no warning. The keyword half simply returns nothing on every long question, and the system
appears to work because vector search still returns results.

**Use OR semantics, ranked by `ts_rank`** — match any significant term and let ranking sort out how
many matched and how rare they were. Rare terms such as clause numbers and route codes score highly
precisely because they are rare, which is this half's entire purpose.

### What the keyword half is for

Not to duplicate semantic search, but to catch what it structurally misses: **exact strings carrying
little semantic weight** — `4.2`, `ABZ`, a form number, a product name. Those mean almost nothing to
an embedding model and are trivially matched by a keyword index.

Worth stating, because if the two halves return the same results, one of them is misconfigured.

---

## 7. The dimension lock

`vector(N)` fixes the dimension at table creation. A different embedding model means a different N,
and it cannot be altered in place — the column is dropped, recreated, and the corpus re-embedded.

Since the embedding model is open (design choice 5.4) and settled by evaluation, this appears to make
each candidate model cost a full rebuild.

**It does not, and the reason is the preamble cache.**

| Stage | Re-runs on a model change? | Cost |
|---|---|---|
| Parse | No — parse cache | — |
| Summarise | No — per document, cached | — |
| Chunk | No — deterministic | — |
| **Contextualise** | **No — keyed on chunk text** | — |
| Embed | Yes | Minutes |
| Build index | Yes | Seconds |

The expensive stage is contextualisation — thousands of model calls, hours of work. It is cached on
chunk *text*, and chunk text does not change when the embedding model does. What remains is embedding
a few thousand chunks and rebuilding an index over them.

**Swapping embedding models is a coffee break, not a migration.**

### So the schema is not engineered around it

The tempting design is a separate `embeddings` table keyed by `(chunk_id, model)`, allowing several
models to coexist for side-by-side comparison. It works, and costs a join on every retrieval plus
permanent complexity, to solve a problem measured in minutes.

**One `embedding` column. Re-embed when the model changes.** Evaluation runs sequentially: index
under model A, measure, re-index under model B, measure.

### When this stops being true

- **Corpus beyond roughly 50k chunks** — re-embedding becomes a meaningful wait
- **Genuine A/B serving** — two models live simultaneously rather than compared offline

Neither applies now, and both become visible well before they hurt.

---

## 8. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Scope | Single store for all persistent state | Transactions, filtered vector search, one service to operate |
| Schema authority | This document | Other components describe only their own contributions |
| Variable metadata | Single `extra` JSON column | Not one column per field |
| Re-ingestion | `ON DELETE CASCADE` from `documents` | Makes delete-then-insert atomic by construction |
| Chunk text | Two columns — `display_text`, `embedding_text` | Never conflated |
| Document summary | Stored on `documents` | Generated once per document, reused per chunk |
| Operator metadata | Fixed columns for what the engine reads; `extra` for the rest | Supplied overrides generated — see [11 · FastAPI](11-fastapi.md) §4 |
| `description` vs `summary` | Two columns, never merged | Regenerating a summary must not overwrite human input |
| `confidentiality` | Column on `documents` | Gates whether the source PDF may be served and deep-linked |
| Operational split | Tracing owns telemetry; this owns durable state | Avoids two sources of truth |
| Ingestion runs | Recorded with full `config` | Makes "what changed?" a lookup rather than archaeology |
| Trace linkage | `messages.trace_id` | The join key between stored state and telemetry |
| Duplicate metrics on `messages` | Latency and token counts stored | Basic questions should not require tracing to be up |
| Vector index | HNSW | Handles incremental inserts; no training pass |
| IVFFlat | Rejected | Requires rebuilds as data changes, with silent recall decay |
| `m` / `ef_construction` | 16 / 64 | Defaults |
| `ef_search` | ~100 | The default of 40 is too close to a `k` of 20 |
| Distance | Cosine, `vector_cosine_ops` | Stated explicitly, not left to a library default |
| Operator match | Verified with `EXPLAIN` | A mismatch silently disables the index |
| Recall validation | Compared against exact search | Turns a parameter guess into a measurement |
| Fusion method | Reciprocal Rank Fusion, k ≈ 60 | Scale-free; score incomparability never has to be resolved |
| Score normalisation | Not used | Fusion feeds the re-ranker, which rescores anyway |
| Where fusion runs | Application, not SQL | Traceability, debuggability, flexibility |
| Filter consistency | Identical filters on both halves | Silent corpus divergence otherwise |
| Text search config | `english` | Revisit for multilingual corpora |
| Keyword query construction | **OR semantics, ranked** | AND returns nothing on natural-language questions, silently |
| Embedding storage | One column, re-embed on model change | The preamble cache makes this cheap |

### Still open

| Item | Decided under | Settled by |
|---|---|---|
| Embedding dimension `N` | Follows the embedding model — design choice 5.4 | Evaluation |
| Retention policy for conversations and run records | This document | Not yet discussed; has a privacy dimension |
| pgvector version pin | This document | Must include iterative index scanning |
| Whether filtering needs partial indexes | This document | Measurement, if filtered retrieval proves heavy |
