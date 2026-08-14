# 01 · Problem Statement

## 1. What is being asked for, in one sentence

A chatbot that answers questions about a set of PDF documents, using only those documents, and
showing where each answer came from.

---

## 2. What makes it more than a basic chatbot

| # | Requirement | Plain meaning |
|---|---|---|
| 2.1 | Grounded answers | It answers from the supplied documents only — not general knowledge, not invention. If the documents do not cover something, it must say so rather than guess. |
| 2.2 | Traceable sources | Every answer points back to a file, a page, a section. A human can verify it without trusting the machine. |
| 2.3 | Conversation memory | Follow-up questions work. *"What about the other one?"* resolves against what was already discussed. |
| 2.4 | Handles hard questions | Not just single lookups. *"How does A compare to B?"* requires two separate searches and a synthesis. This class of question must work. |
| 2.5 | Reasons in steps | Search, assess whether the result is good enough, search again with a better query if not, then answer. Not one blind grab. |
| 2.6 | Ingestion workflow | The documents are prepared before anything can be searched — preprocessing, splitting, vectorization, and indexing. |

Requirement 2.5 is the pivot of the whole system. A conventional retrieval pipeline — embed the
question, fetch the nearest passages, generate — satisfies 2.1 and 2.2 and nothing else. Items 2.4
and 2.5 are what force an agentic design, and they are the reason a multi-agent framework belongs in
the stack at all rather than being an unused dependency.

---

## 3. The constraints

| # | Constraint | Plain meaning |
|---|---|---|
| 3.1 | 100% open source | No commercial model APIs. The language model runs on hardware we control. |
| 3.2 | Observable | When an answer is wrong, we can open the request and see which step failed. |
| 3.3 | Externalised prompts | The instructions given to the model live outside the codebase and can be changed without a code change or redeployment. |
| 3.4 | Measured, not asserted | Quality is reported as scores from a recognised evaluation method, not as an opinion. |

---

## 4. The toolchain

Ten components, each doing a distinct job in the system rather than being present for its own sake.

| # | Tool | Its job in the system |
|---|---|---|
| 1 | **Docling** | Turns PDFs into structured text — headings, tables, reading order — rather than a flat dump |
| 2 | **LlamaIndex + PGVector/PostgreSQL** | The retrieval framework, and the database that stores document chunks and their vectors |
| 3 | **Contextual Agentic RAG** (Anthropic-style) | The retrieval method: embeddings, a language model, and a re-ranking step |
| 4 | **Conversation memory** | Retains dialogue history so follow-ups resolve |
| 5 | **Citation handling via metadata** | Answers carry sources, assembled from stored chunk metadata |
| 6 | **Ollama** | Hosts the language and embedding models locally |
| 7 | **Crew.AI** | Orchestrates the multiple agents that plan, search, answer, and check |
| 8 | **Arize Phoenix** | Prompt lifecycle management **and** tracing/observability — see below |
| 9 | **RAGAs** | Scores the finished system against a held-out test set |
| 10 | **OpenWebUI** (Docker) | The chat interface the user actually types into |

### 4.11 Note on item 8

Item 8 splits into three distinct sub-requirements, which is easy to under-deliver on partially
without noticing — a trace exporter alone looks like observability is "done" while the other two
thirds are silently missing:

- **(a)** Initialize prompts *in* Arize Phoenix
- **(b)** Retrieve prompts *from* Arize when needed in the code
- **(c)** Use Arize for tracing, debugging, and observability

Adding a trace exporter satisfies only (c). (a) and (b) mean prompts must genuinely live in Phoenix
and be fetched at runtime — a registry, not a decoration.

---

## 5. Decisions left open

Ten choices were left open by design, rather than prescribed. Each needed a decision and a recorded
justification, not just a working default:

1. REST API controller and contract
2. Backend code structure
3. Document splitting (chunking) strategy
4. Embedding model
5. Language model on Ollama
6. Retrieval mechanism
7. Re-ranking mechanism
8. Prompts for the crew's agents
9. Integration between OpenWebUI and the backend
10. Deployment structure using Docker and Compose

Each of these gets an entry in [`doc/adr/`](adr/), so the reasoning is recoverable rather than
implied by the code.

---

## 6. Deliverables

| # | Deliverable | Detail |
|---|---|---|
| 6.1 | GitHub repository | Code on the `master` (default) branch |
| 6.2 | `README.md` | Well documented, with deployment instructions, referencing `/doc` |
| 6.3 | `/doc` directory | Design diagrams, presentation deck, supporting technical documents |

---

## 7. Design principles

Two principles shaped every decision in this project, and they matter more than any single
component choice:

- **Completeness over polish in one place.** A system that covers all ten baseline components
  honestly is worth more than one that does two or three of them brilliantly and leaves the rest
  unused or stubbed. §8 exists to make that coverage checkable, not just claimed.
- **Justification is a deliverable, not a courtesy.** Working code with no recorded reasoning is
  only half the job — the other half is *why* each open design choice was made the way it was, and
  what was rejected. That is what `doc/adr/` is for.

### 7.1 Scope position

The system is built **corpus-agnostic**: the engine holds no knowledge of any document domain, and
pointing it at a new document set is a configuration change, not a code change. Development proceeds
against a stand-in corpus, with the design verified to generalize rather than being tuned to one set
of documents.

This is a deliberate widening of scope: a framework that works for any document set is more valuable
to demonstrate than one wired to a single one.

---

## 8. Requirements traceability

Every requirement above, mapped to where it is addressed and verified — nothing below is marked done
without a corresponding artefact: a file, an endpoint, a test, or a live-tested run.

| Req | Requirement | Addressed by | Status |
|---|---|---|---|
| 2.1 | Grounded answers | Verifier agent; retrieval score floor | ✅ Done |
| 2.2 | Traceable sources | Citation assembly from chunk metadata | ✅ Done |
| 2.3 | Conversation memory | Memory store + follow-up query rewriting | ✅ Done |
| 2.4 | Multi-part questions | Planner decomposition into sub-questions; per-sub-question passage allocation | ✅ Done |
| 2.5 | Stepwise reasoning | Sufficiency judgement + bounded retry loop, shared search budget | ✅ Done |
| 2.6 | Ingestion workflow | Parse → summarise → chunk → contextualise → embed → index | ✅ Done |
| 3.1 | 100% open source | Ollama-hosted models; self-hosted services throughout | ✅ Done |
| 3.2 | Observable | Phoenix tracing across all inference calls, serving and ingestion projects separated | ✅ Done |
| 3.3 | Externalised prompts | Phoenix prompt registry, fetched at runtime, three-tier fallback | ✅ Done |
| 3.4 | Measured quality | RAGAs evaluation on a held-out, hand-curated three-way test set (answerable / out-of-scope / near-miss) | ✅ Done |
| 4.1 | Docling | Ingestion — parse stage, cached by file identity | ✅ Done |
| 4.2 | LlamaIndex + PGVector | Ingestion — index stage; retrieval layer (vector + keyword + fusion) | ✅ Done |
| 4.3 | Contextual Agentic RAG | Chunk contextualisation; cross-encoder re-ranking | ✅ Done |
| 4.4 | Conversation memory | *(see 2.3)* | ✅ Done |
| 4.5 | Citation handling | *(see 2.2)* | ✅ Done |
| 4.6 | Ollama | Model hosting — embedding, ingestion, and read-path models | ✅ Done |
| 4.7 | Crew.AI | Agent orchestration — planner, retrieval, synthesizer, verifier as separate agents | ✅ Done |
| 4.8a | Initialize prompts in Phoenix | Startup prompt registration, idempotent | ✅ Done |
| 4.8b | Retrieve prompts from Phoenix | Runtime prompt resolution, pinned per request | ✅ Done |
| 4.8c | Tracing and debugging | *(see 3.2)* | ✅ Done |
| 4.9 | RAGAs | *(see 3.4)* | ✅ Done |
| 4.10 | OpenWebUI via Docker | Frontend service; backend API integration; generation side-effects disabled | ✅ Done |
| 5.1–5.10 | Ten open design choices | One ADR each in `doc/adr/` | ◐ 1 of 10 written |
| 6.1 | GitHub repo on `master` | Repository setup | ✅ Done |
| 6.2 | README with deployment | Root `README.md` — architecture, prerequisites, deployment steps, evaluation | ✅ Done |
| 6.3 | `/doc` supporting material | This directory | ✅ Done |
