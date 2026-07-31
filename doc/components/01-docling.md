# Docling

> Baseline item 1 of 10 — *"Docling — Document processing"*.
> Covers requirement 2.6 (ingestion: preprocessing) and feeds baseline items 3 and 5.

---

## 1. What it is

A document parser. PDF in, structured document out.

A PDF stores instructions for painting marks on a page — it has no notion of sentences, sections, or
tables. Docling **recovers the structure the PDF threw away**, returning three things:

- **Reading order** — the true sequence of content, not the order marks appear on the page
- **Heading hierarchy** — which text is a heading, and what sits beneath it
- **Tables** — as rows and columns, rather than a collapsed stream of numbers

It runs **once per document, at ingestion time only**. It never touches a live user question; by the
time someone types into the chat, Docling's work is finished and stored.

---

## 2. Use-cases covered

| # | Use-case | Purpose |
|---|---|---|
| 1 | Convert PDF → structured document | The primary operation |
| 2 | Recover reading order | Multi-column pages, headers and footnotes don't interleave into nonsense |
| 3 | Keep tables intact | Fees and allowances stay as rows and columns |
| 4 | Supply section hierarchy | Feeds citations (baseline item 5) and the contextual step (baseline item 3) |
| 5 | Supply page numbers | A recorded fact for citations — not a model's guess |
| 6 | Drive document splitting | Chunks follow the document's own structure |
| 9 | Cache parsed output | Docling is the slowest step, and parsing is deterministic |
| 10 | Extract the document title | Displayed in citations and fed to the document summary. An operator-supplied title takes precedence; extraction is the fallback, and the filename the fallback to that — `Conditions of Carriage` beats `coc_v3_final_2024.pdf` |

### Out of scope

| # | Use-case | Reason |
|---|---|---|
| 7 | OCR for scanned PDFs | Assume real-text PDFs until proven otherwise. Note this is *not* settled by "PDF only" — a scanned document is still a PDF. If the client's documents turn out to be scans, this returns as a configuration change. |
| 8 | Non-PDF formats (DOCX, PPTX, XLSX, HTML) | The brief specifies PDF documents |

### Note on the numbering

Docling is not called eight times. It is called **once per document**, and use-cases 2–5 and 10 are
things read off that single result. Only items 1, 6 and 9 are distinct operations.

---

## 3. Workflow

```
Step 1   Discover files          data/raw/*.pdf
Step 2   Hash each file          fingerprint of contents
Step 3   Cache check       [#9]  parsed this exact file before?
              │
        yes ──┴── no
         │         │
         │    Step 4  Convert          [#1, #2, #3]   ← Docling
         │         │
         │    Step 5  Write cache      [#9]
         │         │
         └────►────┘
                   │
Step 6   Chunk                   [#6]                 ← Docling
Step 7   Read metadata           [#4, #5]             ← Docling
Step 8   Hand off  →  contextual step, then embedding
```

| Step | Description |
|---|---|
| 1 · Discover | Find the PDFs to process. The **collection** they belong to is supplied here — a CLI flag or an upload field, defaulting to `default` |
| 2 · Hash | Fingerprint each file's contents — this is what makes the cache trustworthy. A changed file gets a different fingerprint and re-parses; an unchanged one does not |
| 3 · Cache check | If this exact file has been parsed before, load the saved result and skip to step 6 |
| 4 · Convert | The Docling call. Reading order and table handling happen *inside* this, not as separate steps |
| 5 · Write cache | Save the parsed result, keyed by the step-2 fingerprint |
| 6 · Chunk | Split the structured document into chunks |
| 7 · Read metadata | Pull heading path, page number and document title onto each chunk `[#4, #5, #10]` |
| 8 · Hand off | Chunks proceed to the contextual step and embedding. Docling is done |

**Docling touches steps 4, 6 and 7.** Steps 1, 2, 3 and 5 are ours — the caching workflow built
around it.

### Why the cache matters

It splits the pipeline in two. Everything up to step 5 is *expensive and rarely changes*; everything
after is *cheap and changes constantly while tuning*. During development the pipeline is re-run
repeatedly while adjusting chunking — without the cache, every one of those runs re-parses every
document from scratch.

### Where the cache lives

`data/processed/<hash>__<docling-version>.json` — on disk. The version suffix is explained in §5.

The parsed output is **regenerable, not source of truth**. It is never searched, only ever fetched by
exact key, which is precisely what a filename is. Keeping it out of the database avoids bloating
backups and complicating the schema for something no query touches. It also stays honest about what
it is: deleting the directory is a clean rebuild.

Two practical conditions: it must be **mounted from the host** so it survives container restarts, and
it is **gitignored** like all regenerable output.

*(If ingestion ever runs across multiple machines, disk stops working — each machine would hold its
own cache. That would be the point to move it into Postgres or object storage. Not a problem a
single-machine deployment has.)*

### Chunking strategy — step 6

**Docling's `HybridChunker`**

**The decisive reason is provenance.** Page numbers and heading paths exist only inside the
structured document. Splitting anything derived from it — exported markdown or plain text — discards
them, and they cannot be recovered afterwards because the text no longer records where it came from.
Baseline item 5 (citations) depends on them, so citations would degrade to naming whole documents rather than
locations.

The `HybridChunker` operates on the structured document directly, so every chunk emerges still
carrying its heading path and page.

How it works: split along the document's structure first, then merge chunks that came out too small
and split any that came out too large — measured in **tokens**, so chunks respect the embedding
model's limits. Tables stay whole rather than being cut mid-row.

**What this gives up.** *Semantic chunking* — splitting where meaning shifts rather than where the
document says — is not available. That is a real technique and the right one for documents with no
usable structure. Ours are policy-style PDFs with headings and sections; when a document states its
own boundaries, inferring them statistically is a worse answer to a question already answered.

---

## 4. Chunk process

The full sequence from a raw `HybridChunker` output to a record ready for storage.

### Parameters

| Setting | Value | Reasoning |
|---|---|---|
| Tokenizer | The embedding model's own | Token counts must mean the same thing to the chunker as to whatever consumes the chunk, or the limit is fiction |
| `max_tokens` | **768** | Midpoint of the 512–1024 band. A model may accept far more, but a chunk that long dilutes its own embedding — the vector averages several unrelated topics and matches nothing precisely. Small enough to be about one thing; large enough to stand alone. A starting point to tune against evaluation, not a conclusion |
| `merge_peers` | **on** | Merges undersized sibling sections back together |
| Overlap | **none** | Structure-aware splitting cuts at section boundaries, where a mid-sentence break is unlikely. Overlap addresses a problem this approach mostly does not have |
| Minimum chunk | **~100 characters** | Below this it is a heading or page furniture, not content |

### Steps

**1 · Chunk** — `HybridChunker` over the structured document. Output carries text, heading path, and
page.

**2 · Merge, don't drop** — undersized chunks merge into the preceding sibling. Discard only when
there is nothing to merge into. Dropping is silent content loss, and a slightly oversized chunk costs
far less than a missing clause.

**3 · Handle oversized tables** — a table exceeding `max_tokens` splits **by rows**, with the header
row repeated in every part. Without that repetition the second half reads
`| ABZ–LHR | 23kg | 32kg |` with no indication of what the columns mean.

**4 · Build `display_text` and carry the heading path** — the crux of the whole process.

Two texts are eventually stored per chunk:

```
display_text    = the chunk exactly as it appears in the document
                  ── produced here

embedding_text  = heading path
                + contextual preamble        (baseline item 3)
                + display_text
                  ── assembled downstream, once the preamble exists
```

`display_text` is what a citation quotes; `embedding_text` becomes the vector and is never shown to a
user.

**Only `display_text` and the heading path are produced here.** The preamble does not exist yet — it
is generated by the contextual step, which also performs the final assembly. This step's job is to
produce the two ingredients it owns and carry them forward intact.

Including the heading path **as well as** the preamble is deliberate, not redundant. The heading path
is free (~10 tokens), deterministic, and always correct. The preamble is model-generated, so it can
be vague, wrong, or missing when generation fails. Cheap insurance against the unreliable half.

**5 · Deterministic ID** — `chunk_id = hash(doc_hash + position)`. The same document produces the
same IDs on every run, so two ingestion runs are comparable and a chunk can be traced across them. A
random identifier makes that impossible.

**6 · Assemble the record**

```
chunk_id        derived from doc_hash + position
display_text    original text
embedding_text  — not yet set; assembled by the contextual step
source_file     policy-wording.pdf
doc_hash        a3f9c1…
page            14
position        37
extra           { headings: [...], type: "text", token_count: 612 }
```

**7 · Hand off** — the record leaves for the contextual step, which fills in `embedding_text`, and
then for embedding and indexing. Docling's involvement ends here.

### Three rules this rests on

| Rule | Why |
|---|---|
| **Never display `embedding_text`** | It contains text absent from the document. Showing it under a citation is a fabrication with a page number attached |
| **Never embed `display_text` alone** | That discards the context the design exists to add |
| **Merge before you drop** | Every discarded chunk is content the system can no longer find |

> The first rule is the one that fails silently. Evaluation scores the answer, not whether a quoted
> passage matches the source PDF — so it is caught only by a human opening the document and
> checking.

---

## 5. File handling and de-duplication

### How documents arrive

Source PDFs live in `data/raw/`. They arrive either by hand (client documents, which are
confidential) or via a fetch script (public documents, which stay reproducible without being
committed). Either way the directory is gitignored.

Documents are **read, never modified**. Nothing in the pipeline writes back to `data/raw/`, so the
source set is always exactly what was supplied.

### Document identity: content hash

At step 2 each file is fingerprinted with a **SHA-256 over its bytes**. That hash is the document's
identity everywhere downstream.

Hashing content rather than filename or timestamp gives the properties that matter:

| Situation | Result |
|---|---|
| File renamed, content identical | Same hash — nothing reprocesses |
| File touched, content identical | Same hash — nothing reprocesses |
| One character changed | New hash — reparsed and re-indexed |
| Same document supplied twice under different names | Same hash — parsed once |

A filename would fail the first two. A modification timestamp would fail all four, since copying
files changes it.

### The cache key is more than the hash

```
data/processed/<sha256>__<docling-version>.json
```

The Docling version belongs in the key. Upgrading Docling makes every cached parse stale even though
no file changed — without the version, the pipeline would keep loading output from a parser it no
longer runs.

### Two independent checks, not one

"Have we seen this document?" is really two questions, and they drift apart:

| Question | Checked against | If missing |
|---|---|---|
| Is it **parsed**? | `data/processed/` holds the cache file | Run Docling (step 4) |
| Is it **indexed**? | Database holds chunks with this `doc_hash` | Chunk, embed, insert (steps 6–8) |

They come apart routinely — clearing the database leaves the parse cache intact, in which case
parsing is correctly skipped but indexing must still run. Treating it as a single check would
silently index nothing.

### Re-ingesting a changed document

This is the case that causes real damage if handled naively.

A changed file gets a new hash and is correctly reparsed. But **its old chunks remain in the
database under the previous hash** — nothing removes them. The index then holds two versions of the
same document, retrieval can surface either, and the system may cite a superseded clause as if it
were current. For policy or contractual documents that is worse than returning no answer at all.

So re-ingestion is delete-then-insert:

```
for each file:
    hash = sha256(bytes)
    if a document row exists for this source_file with the SAME doc_hash:
        skip                                        ← already current
    else:
        delete the document row                     ← chunks cascade away
        parse (or load cache) → chunk → embed → insert
```

Both facts are needed and both live on the `documents` row: the filename identifies *what to
replace*, the hash identifies *which version it is*. Deleting that row cascades to its chunks, which
is what makes the operation atomic — see 02b.

### The delete and the insert are one transaction

The delete and the re-insert must succeed or fail together.

Between them the document has **no chunks at all**. If the process crashes there, or embedding fails
partway through, the old version is already gone and the new one never arrives — the document
vanishes from the index entirely. Nothing reports it, because from the pipeline's point of view the
file was processed. The next run then sees no chunks for that `source_file` and re-ingests it, so it
self-heals *if* another run happens; until then the system answers questions about that document
with silence.

That is a worse failure than the stale-chunk problem it was introduced to fix, and it is easy to
create while fixing it. Wrapping both statements in a single transaction removes it: on failure the
old chunks are still there, and the document remains answerable at its previous version until
ingestion succeeds.

### Documents removed from the source set

A file deleted from `data/raw/` leaves its chunks in the database indefinitely — it is simply never
seen again. Purging them requires comparing indexed `source_file` values against the files actually
present. Worth doing, and not required for a first working pipeline.

---

## 6. Structure stored in the database

The structure of an arbitrary PDF cannot be known in advance, so the schema separates what is
**always true** from what **varies by document**.

| Field | Type | Source |
|---|---|---|
| `display_text` | fixed column | The chunk exactly as it appears in the document — what citations quote |
| `embedding_text` | fixed column | Heading path + preamble + `display_text` — what becomes the vector. Written by the contextual step, not here |
| `page` | fixed column | Docling `[#5]` |
| `position` | fixed column | Order within the document |
| `extra` | **JSON** | Everything variable |

Fixed columns are for values that always exist and get filtered or sorted on. Everything else goes
into `extra`.

**Document-level facts are not stored on the chunk.** `source_file`, `doc_hash`, and the document
title live on the `documents` row and are reached by join — see 02b. A chunk carries only what is
true of that chunk.

### Why headings live in `extra`

The tempting design is `heading_1`, `heading_2`, `heading_3`. It breaks on the first document with
four levels, and wastes space on every document with one — depth varies from `4 → 4.2` in one
document to `Part II → Ch. 3 → 3.1 → 3.1.a` in another. Stored as a list, one field handles any
depth:

```
["4. What is not covered", "4.2 Exclusions"]
```

### Worked examples

Two chunks from structurally unrelated PDFs, landing in the same shape:

```
display_text  "Cover does not apply where the property has been left
               unoccupied for more than 30 consecutive days."
source_file   policy-wording.pdf
doc_hash      a3f9c1…
page          14
extra         { headings: ["4. What is not covered", "4.2 Exclusions"],
                type: "text" }
```

```
display_text  "| Route | Economy | Business |\n| ABZ–LHR | 23kg | 32kg |"
source_file   baggage-table.pdf
doc_hash      7b2e04…
page          3
extra         { headings: ["Allowances"],
                type: "table", rows: 2, cols: 3 }
```

Same fixed fields; completely different `extra`. Nothing breaks when a new document carries a
field never seen before, and Postgres can still search inside the JSON when needed.

**The discipline this requires:** resist adding a column each time a new field appears. That is how
the schema stops working for the next document set.

> This table is **incomplete by design**. It shows only what this stage contributes and what it
> depends on. The vector column, the keyword-search column, and the indexes over them are defined in
> **02b · PGVector/PostgreSQL**, which holds the complete schema. This table must not be treated as
> the authority on storage.

---

## 7. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| OCR | Off for now | Revisit if the client's documents turn out to be scans |
| Non-PDF formats | Out of scope | Per the brief |
| Parsed-output storage | Disk, `data/processed/` | Regenerable; never queried, only fetched by exact key |
| Document identity | SHA-256 over file bytes | Not filename, not modification timestamp |
| Cache key | Content hash **+ Docling version** | A parser upgrade invalidates every cached parse |
| Skip logic | Two independent checks | Parsed and indexed drift apart; a single check silently indexes nothing |
| Re-ingesting a changed file | Delete all chunks for that `source_file`, then insert | Otherwise two versions coexist and a superseded clause can be cited as current |
| Delete + insert atomicity | Single transaction | A crash between them would erase the document from the index with nothing reporting it — worse than the problem being fixed |
| Document-level fields | On `documents`, not on the chunk | Filename, hash and title are true of the document; chunks join for them |
| Chunker | Docling `HybridChunker` | Part of design choice 5.3. Splitting exported text destroys page numbers — which baseline item 5 (citations) depends on |
| Chunk sizing | ~512–1024 tokens | Not the embedding model's ceiling; an over-long chunk dilutes its own vector. Exact figure to be tuned against evaluation |
| Semantic chunking | Not used | Appropriate for unstructured documents; ours state their own boundaries |
| Chunk overlap | None | Structure-aware splitting cuts at section boundaries, where mid-sentence breaks are unlikely |
| Chunk text | Two texts stored per chunk | `display_text` is quoted in citations; `embedding_text` becomes the vector and is never shown. Conflating them puts text absent from the document under a page citation |
| Heading path in `embedding_text` | Included, alongside the preamble | ~10 tokens, deterministic, always correct — insurance against a model-generated preamble that is vague, wrong, or missing |
| Undersized chunks | Merge into the preceding sibling | Discard only when there is nothing to merge into; every dropped chunk is content the system can no longer find |
| Oversized tables | Split by rows, repeating the header row | Otherwise the later parts have no column meanings |
| Chunk IDs | Deterministic — `hash(doc_hash + position)` | Makes two ingestion runs comparable and lets a chunk be traced across them |
| Document title | Operator-supplied where given; else extracted at parse; else the filename | Consumed by citations and by the document summary; neither has another source for it |
| `embedding_text` assembly | Not performed here | The preamble does not exist at this stage. This step produces `display_text` and the heading path; assembly happens downstream |
| Authority on storage schema | **02b · PGVector/PostgreSQL** | §6 shows only this stage's contributions and is not the complete table |
| Variable metadata | Single JSON column | Not one column per field |
| Redis alongside Postgres | Not adopted | Considered and rejected — Postgres covers the need, and conversation memory requires durability that Redis gives up by default. Revisit only against a measured problem |

### Still open

- Final chunk size within the 512–1024 range (design choice 5.3) — to be settled by evaluation, not
  by argument. Overlap is decided: none
- Purging chunks for documents removed from `data/raw/` — worth doing, not needed for a first
  working pipeline
