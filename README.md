# Document Search Platform

Agentic RAG over a curated PDF corpus. Answers questions using only the supplied
documents, cites where each answer came from, and declines when the corpus does not
cover the question.

> **Status: E1 · Foundation.** The stack runs and reports health. Ingestion, retrieval
> and the agents arrive with epics 4–6 — see [`doc/delivery/`](doc/delivery/).
> This README is completed by E11-S2.

## Quick start

```bash
# 1 · Ollama runs natively — containers on macOS cannot reach the Metal GPU
brew install ollama && ollama serve      # in a separate terminal
make models                              # pulls bge-m3, qwen3:4b, qwen3:8b (~8.7 GB)

# 2 · Everything else
make install
make up
```

| Surface | URL |
|---|---|
| Chat interface | http://localhost:3000 |
| Backend API | http://localhost:8000/docs |
| Traces & prompts | http://localhost:6006 |

## Commands

```bash
make test-unit    # fast — no services required
make test         # everything, including Postgres and Ollama
make lint
make typecheck
make down
```

## Documentation

| | |
|---|---|
| [Problem statement](doc/01-problem-statement.md) | The requirements in plain language |
| [Architecture](doc/02-architecture.md) | **Canonical** — containers, components, both pipelines |
| [Components](doc/components/) | One document per baseline item |
| [Delivery plan](doc/delivery/) | Eleven epics, sequenced for parallel execution |

[`requirements/`](requirements/) holds the brief exactly as received and is never
edited. Everything in `doc/` is authored by us and traces back to it.
