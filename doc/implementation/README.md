# Implementation Records

One document per phase of the build, written as the work completes.

> **Design documents say what should be built. These say what *was* built** — the commands run, the
> results measured, and the problems hit on the way. Where the two disagree, these are the record of
> what actually happened and the design document is the intent.

| Phase | Record | Covers |
|---|---|---|
| Foundation | [01 · Foundation](01-foundation.md) | Repository, configuration, container stack, models, health endpoints |
| Observability & Prompts | [02 · Observability](02-observability.md) | Prompt registry, tracing, verified against a live Phoenix |
| Storage & Schema | [03 · Storage](03-storage.md) | Shared contracts, migrations, fixture corpus, repository layer |
| Ingestion | [04 · Ingestion](04-ingestion.md) | Parse, summarise, chunk, contextualise, embed, write — the whole write path |
| Retrieval | [05 · Retrieval](05-retrieval.md) | Hybrid search, RRF fusion, cross-encoder re-ranking, recall measurement |
| Agentic Read Path | [06 · Agentic Read Path](06-agentic-read-path.md) | Planner, retrieval specialist, synthesizer, verifier; orchestration and decision telemetry |
| Conversation Memory | [07 · Conversation Memory](07-conversation-memory.md) | Token-bounded window, structured summary, and the multi-turn poisoning guards |
| Citations | [08 · Citations](08-citations.md) | Marker collision guard, renumbering, page-level merging, declined-answer near misses |
| API & Frontend | [09 · API & Frontend](09-api-frontend.md) | OpenAI-compatible chat, upload and admin, Celery, and the frontend lockdown |

Every known-open item from these records is collected in
[**Open Items**](OPEN-ITEMS.md) — split into what waits on a later phase, what is
an accepted trade-off, and what is a real limitation.

## What belongs here

- **Verification results, not claims.** Every component records how it was proven working — the query
  run, the value returned, the test that passed.
- **Defects and their root causes.** Including the ones introduced while building, because the root
  cause is usually more instructive than the fix.
- **Decisions made under pressure** that the design documents did not anticipate.

## What does not

Design rationale belongs in [`../components/`](../components/); the argued alternatives belong in
[`../adr/`](../adr/). Duplicating either here guarantees the two will drift apart.
