# Documentation

Supporting technical documentation for the document search platform.

## Contents

| Document | Purpose |
|---|---|
| [01 · Problem Statement](01-problem-statement.md) | The requirements in plain language, and a checklist tracking coverage |
| [02 · Architecture](02-architecture.md) | **Canonical structure and flow** — context, containers, components, both pipelines, deployment, cross-cutting concerns |
| [components/](components/) | One document per baseline item — what it is, the use-cases covered, where it sits in the workflow, and what it stores |
| [implementation/](implementation/) | **Build records** — one per epic: what was built, how each part was verified, and what went wrong |

### Baseline components

The brief's ten baseline items mix two kinds of thing: tools that get installed, and mechanisms that
get built. Both are covered here.

| # | Component | Kind | Status |
|---|---|---|---|
| [01](components/01-docling.md) | Docling | Tool | Discussed |
| [02](components/02-llamaindex.md) | LlamaIndex | Tool | Discussed |
| [02b](components/02b-pgvector-postgresql.md) | PGVector / PostgreSQL | Tool | Discussed — **authority on the storage schema** |
| [03](components/03-contextual-agentic-rag.md) | Contextual Agentic RAG | Mechanism | Discussed |
| [04](components/04-conversation-memory.md) | Conversation memory | Mechanism | Discussed |
| [05](components/05-citation-handling.md) | Citation handling using metadata | Mechanism | Discussed |
| [06](components/06-ollama.md) | Ollama | Tool | Discussed — **authority on model selection** |
| [07](components/07-crewai.md) | Crew.AI | Tool | Discussed — **authority on read-path control flow** |
| [08](components/08-arize-phoenix.md) | Arize Phoenix | Tool | Discussed — **authority on prompts and trace design** |
| [09](components/09-ragas.md) | RAGAs | Tool | Discussed — **authority on evaluation** |
| [10](components/10-openwebui.md) | OpenWebUI | Tool | Discussed — **authority on frontend integration** |

### Additional components

Not in the baseline ten. Permitted by the brief — *"You may also use additional tools of your choice,
where appropriate."*

| Component | Carries | Status |
|---|---|---|
| [FastAPI](components/11-fastapi.md) | Design choices 5.1 (API contract) and 5.2 (code structure); upload surface | Discussed — **authority on the API contract, background tasks, and code structure** |
| Celery + Redis | Ingestion off the request thread; scheduled maintenance | Covered in [11 · FastAPI](components/11-fastapi.md) §5 |

## Convention

[`requirements/`](../requirements/) holds the brief exactly as received and is never edited.
Everything in `doc/` is authored by us and traces back to it.

Each component document describes only its own scope. Cross-component boundaries belong in the
architecture document, not scattered through the component files.
