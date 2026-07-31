# LlamaIndex

> Baseline item 2 of 10 — *"LlamaIndex + PGVector/PostgreSQL — RAG methodology"*.
>
> The baseline bundles a framework and a database into one item. This document covers the
> framework; the database is treated separately.
>
> Covers requirement 2.6 (vectorization, indexing) and serves 2.1, 2.4 and 3.2.

---

## 1. What it is

A framework for building retrieval systems over your own data. It supplies the components that sit
between stored text and a ranked list of relevant passages: embedding integration, vector-store
integration, index management, retrievers, and post-processing.

Its scope in this solution is deliberately bounded:

> **Everything between "here are chunk records" and "here are the best passages for this question."**

It does not decide what to search for, and it does not produce answers. A query goes in, ranked
passages come out, and it stops there.

---

## 2. Use-cases covered

### Storage side — used during ingestion

| # | Use-case | Purpose |
|---|---|---|
| 1 | Vector store connection | PGVector integration — no hand-written SQL for vector storage or similarity search. The keyword half, index configuration and fusion are explicit |
| 2 | Embedding model connection | Turns text into vectors via the locally hosted model |
| 3 | Build nodes from chunk records | Wraps each chunk as text and metadata in one object |
| 4 | Write to the index | Batch embedding and insertion |

### Query side — used when answering

| # | Use-case | Purpose |
|---|---|---|
| 5 | Semantic retrieval | Finds chunks closest in meaning to the question |
| 6 | Keyword retrieval | Catches exact terms, codes and clause numbers that meaning-based search misses |
| 7 | Fuse the two result sets | Combines them into a single ranked candidate list |
| 8 | Re-ranking | A post-processing slot where a cross-encoder rescores candidates against the question |
| 9 | Metadata filtering | Restricts a search by collection, document, or any `extra` field. Applied **inside each query**, never after fusion |

### Integration

| # | Use-case | Purpose |
|---|---|---|
| 10 | Expose retrieval as a callable interface | Retrieval can be invoked on demand and repeatedly, rather than once per question |
| 11 | Emit traces | Satisfies requirement 3.2 — every retrieval and embedding call is recorded |

---

## 3. Workflow

Retrieval appears on **two separate paths**, which is what makes it easy to lose track of.

### Write path — during ingestion

```
Chunk records arrive — already contextualised,
                       embedding_text assembled     (baseline item 3)
   │
1  Build nodes                  [#3]
   │
2  Embed embedding_text         [#2]
   │
3  Insert into PGVector         [#1, #4]
```

Node construction happens **after** contextualisation, not before. A node is the unit handed to
storage, so it is built once, from a chunk record that is already complete. Building nodes first
would mean mutating them afterwards to insert the preamble — the same object assembled twice.

### Read path — when a question arrives

```
Question
   │
1  Embed the question              [#2]
   │
2  ├─ Semantic search  → 20        [#5]  ┐ metadata filters applied
   └─ Keyword search   → 20        [#6]  ┘ INSIDE both queries  [#9]
   │
3  Fuse + dedupe → ~25–30          [#7]
   │
4  Re-rank → keep 5               [#8]
   │
5  Return ranked passages         [#10]
```

### The boundary

**The read path returns passages. It never produces an answer.**

This is what makes requirement 2.5 — search, assess, search again — affordable. Each invocation is a
search rather than a full answer attempt, so it is cheap enough to repeat with different queries
until the evidence is sufficient.

### Why retrieval is hybrid

Semantic search finds text that *means* the same thing as the question, which is what makes natural
questions work at all. It is unreliable on exact strings — a clause number, a route code, a product
name — because those carry little semantic weight and are easily outranked by text that merely reads
similarly.

Keyword search has the opposite profile. Running both and fusing the results covers both failure
modes; running either alone leaves one uncovered.

---

## 4. Node building

A node is the unit written to and read from the index: text, metadata, and identity in one object.
Input is a finished chunk record; output is a node ready to embed and store.

```
Chunk record
   │
1  Assign the node ID          = chunk_id, not a generated one
   │
2  Set node text               = embedding_text
   │
3  Attach metadata             display_text + citation fields + extra
   │
4  Declare metadata visibility what the embedder sees / what a model sees
   │
5  Link neighbours             previous / next
   │
6  Emit node → embed → insert
```

### Practices

**1 · Use our own identifier, never a generated one.**
Nodes are assigned a random identifier by default. That discards the determinism built into the
chunk process — re-running ingestion would produce different identifiers for identical content, so
runs stop being comparable and what should be an update becomes a duplicate insert. Set the node ID
from `chunk_id` explicitly.

**2 · Node text is `embedding_text`; `display_text` travels in metadata.**
Whatever occupies the node's text field is what gets vectorized, so it must be `embedding_text` —
heading path, preamble and original together. The original text therefore has to travel alongside
it, in metadata, and citation and answer-building steps read it from there.

This is the mechanism that keeps the chunk-process rule true: the vectorized text is never the
quoted text.

**3 · Declare metadata visibility explicitly.** ⚠️
Metadata is concatenated into the embedded text by default, and separately into what is handed to a
model. Left at its default, `doc_hash`, `position` and `token_count` are embedded as part of the
vector — noise that shifts every embedding slightly and matches nothing meaningfully.

Both exclusion lists must be set deliberately. `embedding_text` already contains everything worth
embedding, so the embedder needs nothing further from metadata.

**4 · Do not allow anything to re-chunk the nodes.**
Nodes arrive already split, merged and sized against a known tokenizer. Passing them through a node
parser on the way to the index splits them again by different rules, destroying the alignment
between text, token count and page — and with it, the accuracy of every citation.

**5 · Keep metadata small and flat.**
It is stored per node and read on every retrieval. Identifiers, page, heading path and display text;
not large derived blobs.

**6 · Neighbours are derived, not stored.**
Fetching the chunks either side of a match is useful when a passage is clearly mid-thought. It needs
no stored links: `position` and `doc_id` are already columns, so a neighbour is
`WHERE doc_id = ? AND position IN (n-1, n+1)`. Storing explicit previous/next pointers would
duplicate information already present.

**7 · Embed queries with the same model as documents.**
Including any query-versus-document prefix asymmetry the chosen model expects. If that asymmetry is
ignored on either path, every similarity score is subtly wrong in a way nothing will report.

---

## 5. Embedding

Embedding is **purely the embedding model**. One call, text in, vector out — no reasoning, and
nothing to configure beyond the model choice and batch size.

A language model does touch that text, but earlier: writing the contextual preamble during the chunk
process. By the time embedding runs, that preamble is already part of `embedding_text`. The
embedding step itself involves one model and no generation.

---

## 6. Re-ranking

### It is a third model

The re-ranker is not the embedding model. It is a different kind of model doing a different job.

| | Embedding model | Re-ranker (cross-encoder) |
|---|---|---|
| Input | One text at a time | Query **and** passage together |
| Output | A vector | A relevance score |
| Precomputable | Yes — at ingestion | No — needs the question |
| Speed | Fast | Slow |
| Accuracy | Approximate | Substantially better |

That difference is the entire rationale. The embedding model never sees the query and the document
at the same time, so it can only compare them at arm's length through their vectors. The
cross-encoder reads both together and judges relevance directly — far more accurate, and far too
slow to run across a whole index.

Hence the two-stage shape: **cast wide cheaply, then judge precisely.**

### The process

```
Fused candidates (~25)
   │
1  Deduplicate            near-identical chunks waste slots
   │
2  Pair each with query   (query, passage)
   │
3  Score in batches       cross-encoder forward pass
   │
4  Sort by score
   │
5  Apply score floor      drop weak candidates entirely
   │
6  Cut to top-N (~5)
```

### Practices

**1 · Retrieve far more than you keep.**
The re-ranker's value comes entirely from having candidates to sort. Retrieving five and re-ranking
to five accomplishes nothing — it reorders a list that was going to be used anyway. Roughly 25 → 5
is where the gain lives.

**2 · The score floor is what makes "not covered" possible.** ⚠️
The most valuable property here, and the easiest to overlook.

Vector similarity is not calibrated. The nearest chunk to *any* question always scores reasonably
well, even when the corpus contains nothing relevant at all — so a similarity score cannot
distinguish a good match from the least-bad text in the index. Cross-encoder scores are comparable
across queries, which makes an absolute threshold meaningful.

Requirement 2.1 — say so when the documents do not cover something — rests on this.

**3 · Score the same text that was embedded.**
Keeps retrieval and re-ranking judging the same object. There is a reasonable argument for scoring
the original text instead; it is an empirical question, to be settled by evaluation rather than by
reasoning.

**4 · Bound the candidate set.**
Cost is linear in the number of candidates, and this is the slowest step in the read path. Around 25
is comfortable; 100 will be felt on every question.

**5 · Batch the scoring.**
One forward pass per pair, submitted together rather than scored in a loop.

**6 · Prefer a cross-encoder over asking a language model to rank.**
A language model can do this, but it is slower, more expensive, and inconsistent between runs on
identical input. A cross-encoder is purpose-built and produces stable, comparable scores — which
practice 2 depends on entirely.

---

## 7. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Scope | Retrieval layer only | Chunk records in, ranked passages out. No answer generation |
| Retrieval type | Hybrid — semantic + keyword, fused | Each covers the other's failure mode; either alone leaves one uncovered |
| Node identifier | Our deterministic `chunk_id` | A generated identifier turns updates into duplicate inserts |
| Vectorized text | `embedding_text` | `display_text` carried in metadata for citations |
| Metadata in embeddings | Explicitly excluded | Default behaviour embeds internal fields as noise |
| Re-chunking at index time | Disallowed | Would destroy alignment between text, token count and page |
| Neighbour links | **Derived from `(doc_id, position)`** | Storing pointers would duplicate columns that already exist |
| Re-ranking | Cross-encoder | Stable, comparable scores; a language model is slower and inconsistent |
| Candidate window | **20 per half → ~25–30 fused → 5 kept** | The two numbers describe different stages, not a disagreement |
| Weak candidates | Dropped by `keep_floor` | The mechanism behind requirement 2.1 |

### Still open

| Item | Design choice | Settled by |
|---|---|---|
| Embedding model | 5.4 | Evaluation |
| Re-ranker model | 5.7 | Evaluation |
| Fusion method and weighting | 5.6 | Evaluation |
| Final candidate and keep counts | 5.7 | Evaluation |
| Whether re-ranking scores `embedding_text` or the original | 5.7 | Evaluation |
