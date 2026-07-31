# Foundation — Build Record

> The first phase of the build: the skeleton every later part sits inside.
>
> This document records what was built, how each component was *verified* rather than assumed, and
> what went wrong along the way. The design documents say what should be built; this says what was.

---

## 1. What Foundation covers

It contains **no business logic**. Its entire job is proving the substrate works before anything is
built on it.

| Area | Delivers | Status |
|---|---|---|
| Repository | Package layout, dependency management, lint / format / typecheck / test tooling | ✅ |
| Configuration | One resolution point, grouped per concern | ✅ |
| Database | PostgreSQL with pgvector available and new enough | ✅ |
| Model host | Ollama reachable, models present, embedding width verified | ✅ |
| Container stack | Compose, one image run two ways, shared `data/` mount | ✅ |
| Health | Liveness and readiness as separate endpoints | ✅ |

### Explicit non-goals

Each deferred to the phase that owns it:

- **No database schema** — belongs to the storage phase. This phase only proves Postgres starts with
  pgvector present
- **No ingestion pipeline**
- **No API routes beyond health**
- **No tracing or prompt registry**

---

## 2. Repository structure

Established up front and fixed for the rest of the build. Directories exist from the start even when
empty, so the work that fills one never has to invent a location — and independent workstreams never
choose different places for the same thing.

```
document_search_platform/
│
├── app/
│   ├── main.py                   FastAPI assembly, lifespan, configuration logging
│   │
│   ├── api/                      thin — translates HTTP and calls ordinary functions
│   │   └── health.py             liveness and readiness
│   │
│   ├── engine/                   plain Python — NEVER imports fastapi
│   │   ├── ingest/               parse → summarise → chunk → contextualise → embed
│   │   └── query/                agents · retrieval · memory · citations
│   │
│   ├── shared/                   used by every layer
│   │   ├── config.py             one settings class per concern
│   │   ├── health.py             dependency probes
│   │   ├── models.py             Ollama client
│   │   └── store/                schema, queries, session handling
│   │
│   ├── tasks/                    Celery entry points — thin wrappers over the engine
│   │   └── celery_app.py         queues, beat schedule, no result backend
│   │
│   └── static/                   operator admin page
│
├── eval/                         evaluation harness — CLI only, never served
│
├── tests/
│   ├── unit/                     fast — no services required
│   └── integration/              marked; needs Postgres and/or Ollama
│
├── migrations/                   Alembic — schema is versioned, never ad hoc
├── prompts/                      bundled prompt files, fallback when Phoenix is unreachable
│
├── data/                         bind-mounted into backend AND worker
│   ├── raw/                      source PDFs — gitignored
│   ├── processed/                Docling parse cache — gitignored
│   └── preambles/                contextual preamble cache — gitignored
│
├── doc/
│   ├── 01-problem-statement.md   requirements in plain language
│   ├── 02-architecture.md        CANONICAL — structure and flow
│   ├── components/               one per baseline item
│   ├── implementation/           build records — this document
│   ├── adr/                      one per open design choice
│   ├── diagrams/
│   └── presentation/
│
├── scripts/init-db.sql           creates the pgvector extension on first init
├── docker-compose.yml            six services
├── Dockerfile                    one image, run two ways
├── Makefile
├── pyproject.toml                dependencies, ruff, mypy, pytest
└── .env.example
```

### Two structural rules the layout encodes

**`engine/` never imports `fastapi`.** Ingestion runs as a Celery task *and* as a CLI; evaluation runs
only as a CLI. If engine code reached for request context, neither would work — and `eval/`, which
drives the engine directly, would need a running web server to measure anything.

**`tasks/` is a caller, not a layer.** Celery tasks are thin wrappers over engine pipelines, exactly as
API routes are. The same work is reachable three ways — endpoint, task, CLI — precisely because none
of the three owns it.

### Module ownership

Each area of the system owns a distinct part of the tree, so independent work never collides:

| Directory | Owned by |
|---|---|
| `pyproject.toml` · `docker-compose.yml` · `Dockerfile` · `Makefile` · `app/main.py` · `app/shared/config.py` | Foundation |
| `app/shared/tracing.py` · `app/shared/prompts.py` · `prompts/` | Observability & prompts |
| `app/shared/store/` · `app/shared/types.py` · `migrations/` | Storage & schema |
| `app/engine/ingest/` | Ingestion |
| `app/engine/query/retrieval.py` | Retrieval |
| `app/engine/query/agents.py` · `pipeline.py` | Agentic read path |
| `app/engine/query/memory.py` | Conversation memory |
| `app/engine/query/citations.py` | Citations |
| `app/api/` · `app/tasks/` · `app/static/` | API & frontend |
| `eval/` | Evaluation |

⚠️ **`app/shared/config.py` is the one file every area must touch**, since each adds settings. It is
structured as one class per concern rather than a flat namespace specifically so that adding a concern
means adding a block, not editing a shared list — otherwise it becomes the file every change conflicts
on.

---

## 3. Components: steps executed and how each was verified

The governing rule throughout: **a step ends in a demonstration, not a claim.** Every row below records
a measured result, not an assertion that something "was set up".

Services were brought up **one at a time and verified individually**, rather than with a single
`docker compose up`. When six services start together and one misbehaves, the failure has six
candidate causes; started singly, each has one.

> Commands use **`docker compose`** (v2, a Docker subcommand), not the older standalone
> `docker-compose` binary. The two differ in more than spelling — v2 supports the `depends_on`
> health conditions and profiles this stack relies on.

### 3.1 Repository and dependencies

**Steps**

1. Package layout per [11 · FastAPI §7](../components/11-fastapi.md) — `api/`, `engine/`, `shared/`,
   `tasks/`, `eval/`
2. `pyproject.toml` with base dependencies plus optional groups (`ingest`, `retrieval`, `agents`,
   `observability`, `evaluation`)
3. Tooling: **ruff** (lint + format), **mypy** in strict mode, **pytest** with `integration` and
   `models` markers
4. `Makefile` covering install, lint, typecheck, test, up, down, models, seed, ingest

```bash
uv sync --group dev        # install base + dev tooling
uv lock                    # resolve EVERYTHING, including all optional groups
```

**Verified**

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -m "not integration and not models" -q
```

| Check | Result |
|---|---|
| Full dependency resolution | **347 packages, zero conflicts** |
| Coexistence of the heavy stacks | docling 2.115 · crewai 1.15.8 · llama-index-core 0.14.23 · ragas 0.4.3 · arize-phoenix 14.10 · torch 2.13 |
| `ruff check` | clean |
| `ruff format --check` | clean |
| `mypy app` (strict) | **no issues, 14 source files** |
| Unit tests | 9 passed |

> Resolving all five optional groups together was the single biggest unknown in this phase. These
> libraries pin overlapping transitive dependencies and frequently conflict. Confirming they coexist
> **before** writing code removed the risk of discovering it later, separately, in several
> workstreams at once.

### 3.2 Configuration

**Steps**

1. Eight settings classes — `DatabaseSettings`, `OllamaSettings`, `IngestionSettings`,
   `RetrievalSettings`, `AgentSettings`, `ConversationSettings`, `PhoenixSettings`, `CelerySettings`
2. Nested environment binding via `env_nested_delimiter="__"` (`DB__HOST`, `RETRIEVAL__KEEP_FLOOR`)
3. `.env.example` documenting every setting
4. Resolved configuration logged at startup, secrets omitted

**Verified**

```
✓ Defaults trace to the component documents:
    ef_search=100  ·  rrf_k=60  ·  search_budget=6  ·  sub_question_cap=4
    embedding_dimension=1024  ·  num_ctx=8192  ·  temperature=0.0
✓ DB__HOST override applied; sibling db.port kept its default
✓ celery.ingestion_concurrency == 1
✓ celery.ignore_result is True
✓ phoenix.required_for_readiness is False
```

### 3.3 PostgreSQL + pgvector

**Steps**

1. `pgvector/pgvector:pg16`, named volume `pgdata`
2. `scripts/init-db.sql` creating the extension on first initialisation only — tables belong to
   Alembic migrations, so schema stays versioned rather than applied by a script that runs once
3. Startup assertion of the minimum pgvector version

```bash
docker compose up -d postgres

# block until the container's own healthcheck passes, rather than guessing at a sleep
until docker compose ps --format '{{.Service}} {{.Health}}' | grep -q "postgres healthy"; do
  sleep 2
done
```

**Verified**

```bash
docker compose exec -T postgres psql -U rag -d rag \
  -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
```

```
 extname | extversion
---------+------------
 vector  | 0.8.4          ← required ≥ 0.8.0

server_version → 16.14
```

```bash
uv run pytest -m integration -q        # 1 passed
```

> **Why the version check is not ceremony.** Filtered vector search depends on *iterative index
> scanning*, added in pgvector 0.8.0. On an older build the query returns **fewer rows than
> requested** — silently, with no error. That surfaces much later as an unexplained retrieval-quality
> problem, so it is asserted at startup instead.

### 3.4 Redis

**Steps** — `redis:7-alpine` with a `redis-cli ping` healthcheck.

```bash
docker compose up -d redis
docker compose exec -T redis redis-cli ping
```

**Verified** — `PONG`; container reports healthy.

### 3.5 Arize Phoenix

**Steps** — `arizephoenix/phoenix:latest`, named volume `phoenixdata`, registered in readiness as a
**non-required** dependency.

```bash
docker compose up -d phoenix
until curl -sf http://localhost:6006 >/dev/null; do sleep 3; done
```

**Verified** — UI reachable on `:6006`; readiness still reports `ready: true` when Phoenix is down.

> **Non-required is deliberate.** An observability outage must not become an availability outage.

### 3.6 Ollama

Runs **natively**, not in Compose, per [02 · Architecture §6](../02-architecture.md): containers on
macOS cannot reach the Metal GPU, and a CPU-only model host is unusable for ingestion's per-chunk work.

**Steps** — start the daemon; pull models in the order later phases need them.

```bash
ollama serve &                    # detached; the stack reaches it at host.docker.internal:11434

# `set -o pipefail` matters here — see §5.2. Without it a failed pull reports success.
set -o pipefail
ollama pull bge-m3                # 1.2 GB
ollama pull qwen3:4b              # 2.5 GB
ollama pull qwen3:8b              # 5.0 GB
ollama list
```

**Verified**

| Model | Size | Used for | Status |
|---|---|---|---|
| `bge-m3` | 1.2 GB | Embedding — fixtures and retrieval | ✅ |
| `qwen3:4b` | 2.5 GB | Contextualisation, summarisation, verification | ✅ |
| `qwen3:8b` | 5.0 GB | Answering | ✅ |

**The assertion that mattered most:**

```
bge-m3 embedding width  →  1024 dimensions  (matches configuration)
```

> `vector(N)` is fixed when the chunks table is created. A mismatch discovered after ingestion means
> dropping the column and re-embedding the entire corpus — so it is asserted here, before any schema
> exists.

### 3.7 Backend and worker containers

**Steps**

1. One image from `python:3.12-slim`, run two ways — `uvicorn` for the backend,
   `celery worker --beat` for the worker
2. Explicit shared image name so the Dockerfile is built **once**
3. `data/` bind-mounted identically into both
4. Ingestion queue pinned to **concurrency 1**
5. Base dependencies only in the image; heavy extras added on demand via a build argument

```bash
docker compose build backend
docker compose up -d backend worker

# When the image exists but the registry is unreachable, skip the rebuild entirely:
docker compose up -d --no-build backend worker
```

**Verified**

```bash
curl -s http://localhost:8000/health
docker compose exec -T worker sh -c \
  'celery -A app.tasks.celery_app inspect ping -d celery@$HOSTNAME'
docker compose ps --format '{{.Service}}\t{{.State}}\t{{.Health}}'
```

```
backend   running   healthy      /health → {"status":"alive"}
worker    running   healthy      celery inspect ping → pong
                                 queues: ingestion, default
                                 beat: started
```

> **Concurrency 1 is load-bearing, not conservative.** The ingestion pipeline batches by model — all
> summaries, then all preambles, then all embeddings. Two concurrent runs reintroduce the model thrash
> that batching exists to prevent, from *outside* the pipeline where nothing inside it can attribute
> the cause.

### 3.8 Health endpoints

**Steps** — `/health` checks nothing external; `/health/ready` probes each dependency and names the
failing one.

**Verified**

```
GET /health        → 200  {"status":"alive"}

GET /health/ready  → 503   (while models were still downloading)
{"ready": false, "dependencies": [
  {"name":"postgres","status":"ok","detail":"pgvector 0.8.4","required":true},
  {"name":"ollama","status":"unavailable","detail":"missing models: qwen3:4b, qwen3:8b","required":true},
  {"name":"phoenix","status":"ok","required":false}]}
```

That 503 was the acceptance criterion **passing**, not a defect: the endpoint correctly detected the
absent models and named the responsible dependency.

> **Two endpoints, not one.** Liveness answers *"should this be restarted?"*; readiness answers
> *"should this receive traffic?"*. Collapsed into one, a backend that cannot reach the model host
> either looks healthy — and fails every request — or is restarted repeatedly, which fixes nothing.

---

## 4. Final state

```
CONTAINERS          backend healthy · worker healthy · postgres healthy
                    redis healthy   · phoenix running

MODELS              bge-m3 · qwen3:4b · qwen3:8b

QUALITY GATES       ruff clean · ruff format clean · mypy strict clean
                    12 / 12 tests passing
```

**Deferred by design:** the OpenWebUI image, until the frontend phase needs it.

---

## 5. Challenges and how they were resolved

### 5.1 Environment issues

| Problem | Symptom | Root cause | Resolution |
|---|---|---|---|
| **Corrupt Ollama partials** | Five pull attempts failed in ~12 s each with `Error: EOF`, against a demonstrably working network | Blob directory held **zero-byte `-partial-N` files**; Ollama resumed from them indefinitely | Cleared the partials, re-pulled clean — succeeded first attempt |
| **Postgres major-version clash** | `FATAL: database files are incompatible with server` | A pg16-initialised volume met a pg17 server | Recreated on pg16; added a Compose comment so the trap is visible |
| **Docker Hub TLS timeout** | `failed to resolve source metadata for python:3.12-slim` | Transient registry failure during a rebuild that was not needed | Used `--no-build` — the image already existed |
| **DNS during network handover** | `lookup registry.ollama.ai: no such host` | Pull attempted before DNS settled after a connection switch | Retry loop; the next attempt connected |

> ⚠️ **The most expensive lesson.** The corrupt-partial failure *looked* exactly like a bandwidth
> problem and was treated as one for roughly half an hour, including switching networks twice. The
> tell was in the numbers and was missed: each attempt failed in **~12 seconds** having transferred
> **zero bytes**, which no bandwidth limitation produces. A slow link fails slowly; this failed
> instantly. **Check the failure's *shape* before accepting the obvious explanation.**

### 5.2 Defects in the build itself

| Problem | Root cause | Fix |
|---|---|---|
| Worker had no image | The YAML anchor gave each service its own derived image name, building the same Dockerfile **twice** | Explicit shared `image: document-search-platform:local` |
| Worker reported `unhealthy` | It inherited the image's **HTTP** healthcheck, but runs Celery, not uvicorn — the probe could never pass | Overrode with `celery inspect ping` |
| Healthcheck still failed | Written as exec-form `CMD`, which performs **no shell expansion**, so `$HOSTNAME` reached Celery literally | Switched to `CMD-SHELL` |
| A failed pull reported success | `ollama pull … \| tr \| tail` returns **tail's** exit status, masking the failure | `set -o pipefail` and an explicit `if` |
| Ollama daemon died mid-work | Stopping a chained task also killed its child `ollama serve` | Restarted detached |
| Downloads crawled | Three large downloads run **in parallel** over one constrained link | Serialised |

### 5.3 Process corrections

| Problem | Correction |
|---|---|
| **Architecture drift** — Ollama was briefly moved into Compose after a misread instruction | Caught in review and fully reverted: six containers, Ollama native, `02-architecture.md` restored to the designed deployment |
| ~12 GB of downloads on a metered connection | Scope cut to actual need, deferring the frontend image and the two generative models until the phases that use them. Immediate requirement fell to ~1.5 GB |
| Docker image carried the full ML stack | Base dependencies only, extras added on demand via build argument — ~3 GB smaller and rebuilds in seconds |

---

## 6. What this unblocks

| Phase | Ready? |
|---|---|
| **Observability & prompts** | ✅ Phoenix running; needs no model |
| **Storage & schema** | ✅ Postgres verified; `bge-m3` present for fixture embeddings |
| **Ingestion** | ✅ `qwen3:4b` present |
| **Agentic read path** | ✅ `qwen3:8b` present |
| **API & frontend** | ⏸️ OpenWebUI image still to pull |

---

## 7. Command reference

```bash
make install      # resolve and install, including dev tooling
make up           # bring up the Compose stack
make models       # pull the three models into the native Ollama
make test-unit    # fast subset — no services required
make test         # everything, including Postgres and Ollama
make lint
make typecheck
make down
```

| Surface | URL |
|---|---|
| Backend API | http://localhost:8000/docs |
| Health | http://localhost:8000/health/ready |
| Traces and prompts | http://localhost:6006 |
| Chat interface *(later phase)* | http://localhost:3000 |
