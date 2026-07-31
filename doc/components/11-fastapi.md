# FastAPI

> Not one of the baseline ten. Permitted by the brief — *"You may also use additional tools of your
> choice, where appropriate."*
>
> **This document is the authority on the API contract, background task infrastructure, and code
> structure** — design choices 5.1 and 5.2.
>
> Serves requirements 2.6 (ingestion workflow) and 3.2, and carries the integration surface described
> in [10 · OpenWebUI](10-openwebui.md).

---

## 1. What it is

A Python web framework. Here it is deliberately **thin**:

> **The API layer translates HTTP into calls on ordinary functions, and back. Nothing below it knows
> it is being served.**

That constraint is not stylistic. Ingestion runs both as an endpoint and as a CLI; evaluation runs
only as a CLI. If engine code reached for request context, neither would work — and the evaluation
harness, which drives the engine directly, would need a running web server to measure anything.

---

## 2. Use-cases covered

| # | Use-case | Surface |
|---|---|---|
| 1 | Serve the chat interface | OpenAI-compatible endpoints, streamed |
| 2 | Accept document uploads with metadata | `POST /documents` |
| 3 | Report ingestion progress | `GET /ingestion-runs/{id}` |
| 4 | List and remove documents | `GET`, `DELETE /documents` |
| 5 | Provide an operator surface | `GET /admin` |
| 6 | Run ingestion off the request thread | Celery + Redis |
| 7 | Expose retrieval without generation | `POST /search` — debugging and evaluation |
| 8 | Report health | Liveness and readiness, separately |

---

## 3. The API contract

### Chat — consumed by OpenWebUI

```
GET   /v1/models                    the two profiles
POST  /v1/chat/completions          SSE stream
```

Shape and semantics belong to [10 · OpenWebUI](10-openwebui.md), which is the authority on frontend
integration. Two points matter here:

- **Streaming is not optional.** With a 20–60 s answer, a non-streaming response is a blank screen for
  the whole duration. Progress is emitted as reasoning content so the user sees planning, searching
  and verifying as they happen.
- **Conversation identity arrives in a header**, `X-Conversation-Id`, with a hash fallback when
  OpenWebUI does not send one.

### Documents

```
POST   /documents                   multipart/form-data
GET    /documents                   list, with per-document status
DELETE /documents/{id}              removes document; chunks cascade
```

`POST /documents` fields:

| Field | Required | Notes |
|---|---|---|
| `file` | ✓ | The PDF |
| `collection` | ✓ | Corpus isolation |
| `title` | | Overrides the extracted title |
| `description` | | Overrides the generated summary; feeds the corpus description |
| `effective_date` | | Which version applies |
| `confidentiality` | | `public` \| `internal` \| `confidential` — default `internal` |
| `extra` | | JSON object — anything corpus-specific |

```
→ 202 Accepted
  { document_id, ingestion_run_id, status: "queued" }

→ 200 OK
  { document_id, status: "duplicate" }
```

**202, not 201.** The work is queued, not done. A 201 would claim a resource exists in a searchable
state when parsing has not started.

**The hash is computed at the door, synchronously.** It costs well under a second on a PDF and lets a
re-upload of an unchanged file return `duplicate` immediately, without occupying the queue. The one
piece of work that belongs on the request thread is the one that decides whether there is any work.

### Ingestion runs

```
GET   /ingestion-runs/{id}          status, counts, progress
GET   /ingestion-runs               recent runs
```

Read directly from the `ingestion_runs` table — the record already exists for reproducibility, so
progress reporting costs nothing extra.

### Operator surface

```
GET   /admin                        a single self-contained HTML page
```

### Diagnostics

```
POST  /search                       retrieval only, no generation
GET   /health                       liveness  — is the process up
GET   /health/ready                 readiness — are Postgres and Ollama reachable
```

**Two health endpoints, not one.** Liveness answers *"should this be restarted?"*; readiness answers
*"should this receive traffic?"*. Collapsing them means a backend that cannot reach the model host
either looks healthy — and fails every request — or gets restarted repeatedly, which fixes nothing.

`POST /search` returns the ranked passages with their scores and no model output. It is how retrieval
quality is inspected without the synthesizer's wording in the way, and what the evaluation harness
uses for retrieval-only metrics.

---

## 4. Upload metadata

### The principle

> **Operator-supplied metadata is authoritative. Generated metadata is the fallback.**

The same shape as prompt resolution: bundled defaults are a fallback, the registry is the truth.

| Given | Absent |
|---|---|
| `title` used | Extracted at parse, else the filename |
| `description` becomes the document summary | Summary generated from title, heading tree and first pages |
| `effective_date` recorded | Ignored |
| `confidentiality` applied | Defaults to `internal` |

⚠️ **Every field except `file` and `collection` is optional, and must stay optional.** The
corpus-agnostic promise is *"point it at PDFs and it works."* Metadata makes it work better; the
moment it becomes a precondition, that promise is gone.

### The corpus description

The reason `description` earns a column rather than a slot in `extra`.

The planner classifies questions as out-of-scope **with no knowledge of what the corpus contains** —
it is inferring from the question alone. *"What is the weather"* is easy; *"what is the surfboard
fee"* is only out of scope if the corpus happens not to cover sports equipment, and nothing tells the
planner either way.

Document descriptions assemble into a corpus description, supplied to the planner:

```
This corpus covers:
  · Passenger conditions of carriage — ticketing, changes, liability
  · Checked and cabin baggage allowances, including sports equipment
  · Etihad Guest programme terms
```

Classification stops being a guess. It moves the metric that has been hardest to move — declining
correctly, which is half of requirement 2.1.

Where no description is supplied the generated summary takes its place, so the corpus description
exists either way; supplying descriptions makes it authoritative rather than inferred.

### Where the fields live

Following the rule in [02b · PGVector/PostgreSQL](02b-pgvector-postgresql.md): **fixed columns only
for what the engine actually reads.**

| Engine reads it | Column on `documents` |
|---|---|
| Corpus isolation | `collection` |
| Citation display | `title` |
| Corpus description, summary override | `description` |
| Version applicability | `effective_date` |
| Whether the PDF may be served or deep-linked | `confidentiality` |

Document type, jurisdiction, language, department, anything else — **`extra`**. Adding a column per
field is how the schema stops working for the next document set.

`confidentiality` settles an item that had been left open with no owner: whether answers may
deep-link into the source PDF at `#page=14`. A document marked `public` may be served and linked; one
marked `confidential` is cited by name and page but never served. The document now carries its own
answer.

---

## 5. Background tasks

### Why the request thread cannot do it

Ingestion is minutes to hours — parse, one summary call per document, one preamble call per chunk,
then embedding. No HTTP request survives that, and no operator should hold a browser tab open for it.

### Celery + Redis

```
POST /documents  ──►  enqueue  ──►  Redis  ──►  Celery worker
      │                                              │
      └──► 202 + ingestion_run_id                     └──► ingestion_runs (progress)
                    │                                              │
                    └──────────── GET /ingestion-runs/{id} ◄────────┘
```

**Progress is read from Postgres, not from Celery.** The worker updates `ingestion_runs` as it goes —
the table that already exists for reproducibility. The API never asks the broker anything.

That is why the configuration sets:

```python
task_ignore_result = True  # no result backend
```

With no result backend there is no second, competing record of what a job is doing. One source of
truth, enforced structurally rather than by discipline.

### Queues

| Queue | Concurrency | Carries |
|---|---|---|
| `ingestion` | **1** | Document ingestion |
| `default` | 2–4 | Retention cleanup, orphan detection, chunk purges |

⚠️ **Ingestion concurrency is 1, and this is load-bearing.** The pipeline batches by model — all
summaries, then all preambles, then all embeddings — because interleaving would swap models in and
out of memory thousands of times, turning a twenty-minute run into a fourteen-hour one with correct
output throughout. Two concurrent runs reintroduce exactly that thrash from outside the pipeline,
where nothing in the pipeline can see it.

### Scheduled work — Celery beat

| Task | Addresses |
|---|---|
| Retention cleanup | Conversations and run records grow without bound |
| Orphaned run detection | A run left `running` after a worker crash never resolves itself |
| Purge chunks for removed documents | Files deleted from `data/raw/` still have chunks indexed |

Three items previously recorded as open with nothing to execute them.

### What is not deployed

**Flower.** A Celery monitoring UI, and a fourth service to run for information already available in
two places: Phoenix holds the ingestion traces, `ingestion_runs` holds the durable state. Adding it
would mean a third view of the same facts.

### Memory cost

| | |
|---|---|
| Redis | ~100 MB — a queue of job descriptors, nothing large |
| Celery worker | ~1–2 GB — **Docling's layout models load here**, not in the API process |

The worker is where parsing happens, so this is where that cost moves to. It does not add to the
budget; it relocates it out of the request-serving process, which is an improvement.

### ⚠️ The worker needs the same volume

The API writes the uploaded file; the worker reads it. Both must mount `data/` at the same path. This
is the failure that looks like a bug in Docling — a `FileNotFoundError` on a file the API demonstrably
just wrote, because the two containers disagree about where `data/` is.

---

## 6. The admin page

### Why the frontend cannot serve it

OpenWebUI's file upload is not a generic control — it is wired into OpenWebUI's **own** retrieval
stack: its own extractor, its own chunker, its own vector store. There is no hook redirecting the file
to an external URL, and no extension point for a custom page; Tools and Functions act on chat turns,
not on file management.

A document uploaded there enters OpenWebUI's index and this pipeline never sees it. No Docling, no
contextual preamble, no citations — and the answer still looks plausible, which is the part that makes
it dangerous.

> This is why [10 · OpenWebUI](10-openwebui.md) §8 requires its upload to be **disabled**. Not
> tidiness — an operator using the paperclip out of habit gets a silently second-rate system with no
> indication anything is wrong.

### The framing

**The API is the contract; the page is one client of it.**

```
POST /documents  ← the interface
      ↑
      ├── admin page
      ├── curl / CI script
      └── anything later
```

Whoever renders the form is an implementation detail, and nothing else in the design depends on it.

### What it is

One self-contained HTML file at `app/static/admin.html`, served by `GET /admin`. Upload form, a
document table, and a progress row polling `GET /ingestion-runs/{id}`.

No build step, no framework, no second deployable. It is an operator tool used by one or two people;
a build pipeline would earn nothing.

⚠️ **No authentication.** The stack binds to localhost and has no auth layer anywhere — noted here
because an upload-and-delete surface is where that assumption stops being harmless. Any deployment
beyond a single machine needs this addressed before this endpoint is exposed.

---

## 7. Code structure

Design choice 5.2. The layout follows the component diagram in
[02 · Architecture](../02-architecture.md) §3 directly.

```
app/
  main.py                    app assembly, lifespan, middleware
  api/                       thin — HTTP in, HTTP out
    chat.py                  /v1/models, /v1/chat/completions
    documents.py             POST, GET, DELETE /documents
    runs.py                  /ingestion-runs
    admin.py                 GET /admin
    search.py                POST /search
    health.py                /health, /health/ready
    schemas.py               pydantic models — the contract, in one place
  engine/                    plain Python — never imports fastapi
    ingest/
      parse.py               Docling
      summarise.py           per document
      chunk.py               HybridChunker
      contextualise.py       per chunk
      index.py               embed + write
      pipeline.py            the run, and its ingestion_runs record
    query/
      agents.py              planner · retrieval · synthesizer · verifier
      retrieval.py           hybrid · fuse · rerank
      memory.py
      citations.py
      pipeline.py            the turn
  shared/
    config.py                settings, one source
    store/                   schema, queries, session handling
    prompts.py               Phoenix registry + bundled fallbacks
    tracing.py               OTLP setup, span helpers
    models.py                Ollama clients
  tasks/
    celery_app.py            configuration, queues, beat schedule
    ingestion.py             calls engine.ingest.pipeline
    maintenance.py           retention, orphans, purges
  static/
    admin.html
eval/                        CLI only — never served
```

**What to notice**

**`engine/` never imports `fastapi`.** The rule that makes ingestion runnable as a CLI, evaluation
runnable without a server, and the whole engine testable without HTTP.

**`tasks/` is a caller, not a layer.** Celery tasks are thin wrappers over engine pipelines, exactly
as API routes are. The same work is reachable three ways — endpoint, task, CLI — because none of the
three owns it.

**`schemas.py` holds the contract in one file.** The API surface is readable in a single place rather
than reconstructed from six routers.

**The write path and read path are separate packages** — different models, different stages, different
failure modes. They meet only at `shared/store/`.

---

## 8. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| API layer | Thin — translates HTTP only | Engine never imports the web framework |
| Upload | `POST /documents`, multipart | Metadata fields alongside the file |
| Upload response | **202 Accepted** + `ingestion_run_id` | Work is queued, not done |
| Duplicate detection | Hash computed synchronously, at the door | Re-upload returns immediately, never queues |
| Metadata authority | **Supplied overrides generated** | Same pattern as prompt fallbacks |
| Metadata requirement | All optional except `file`, `collection` | Preserves the zero-configuration path |
| `description` | Fixed column — feeds summary **and** corpus description | The engine reads it; that is what earns a column |
| `confidentiality` | Fixed column — gates PDF serving and deep-linking | Resolves an open item that had no owner |
| Everything else | `extra` JSONB | A column per field breaks the next corpus |
| Background tasks | **Celery + Redis** | Ingestion cannot run on a request thread |
| Result backend | **None** — `task_ignore_result=True` | Prevents a second, competing record of job state |
| Progress source | `ingestion_runs` in Postgres | The table already exists for reproducibility |
| Ingestion concurrency | **1** | Concurrent runs reintroduce model thrash from outside the pipeline |
| Scheduled work | Celery beat | Retention, orphaned runs, chunk purges |
| Flower | Not deployed | Phoenix and `ingestion_runs` already cover it |
| Upload surface | FastAPI serves `GET /admin` | OpenWebUI has no extension point that bypasses its own retrieval |
| OpenWebUI upload | **Disabled** — enforced | Otherwise it silently bypasses the entire pipeline |
| Admin page | Single static HTML, polls for progress | Two-operator tool; a build pipeline earns nothing |
| Health | Liveness and readiness, separately | One endpoint cannot answer both questions |
| Retrieval inspection | `POST /search` | Retrieval quality without the synthesizer in the way |
| Code structure | `api/` · `engine/` · `shared/` · `tasks/` · `eval/` | Mirrors the component diagram |

### Still open

| Item | Settled by |
|---|---|
| Authentication on `/admin` and `/documents` | Any deployment beyond a single machine |
| Retention periods for conversations and run records | Not yet discussed; has a privacy dimension |
| Whether `effective_date` drives retrieval filtering or is recorded only | Whether the client's corpus carries superseded versions |
| Rate limiting | Not required at single-operator scale |
