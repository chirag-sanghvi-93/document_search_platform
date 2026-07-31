# 01 · Problem Statement

> **Sources.** This document restates, in plain language, the requirements as received in
> [`requirements/notes.txt`](../requirements/notes.txt) and the assignment brief image in the same
> directory. Those files are the authority and are never edited. Where this document and the source
> disagree, the source wins.

---

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
| 2.6 | Ingestion workflow | The documents are prepared before anything can be searched — preprocessing, splitting, vectorization, and indexing. Stated explicitly in `notes.txt`. |

Requirement 2.5 is the pivot of the whole assignment. A conventional retrieval pipeline — embed the
question, fetch the nearest passages, generate — satisfies 2.1 and 2.2 and nothing else. Items 2.4
and 2.5 are what force an agentic design, and they are the reason a multi-agent framework appears in
the mandated tool list at all.

---

## 3. The constraints

| # | Constraint | Plain meaning |
|---|---|---|
| 3.1 | 100% open source | No commercial model APIs. The language model runs on hardware we control. |
| 3.2 | Observable | When an answer is wrong, we can open the request and see which step failed. |
| 3.3 | Externalised prompts | The instructions given to the model live outside the codebase and can be changed without a code change or redeployment. |
| 3.4 | Measured, not asserted | Quality is reported as scores from a recognised evaluation method, not as an opinion. |
| 3.5 | Prescribed toolchain | Ten named tools must be used. This is not advisory — see §4. |

---

## 4. The mandated toolchain

Ten items, listed in the brief as "The Baseline". Each is restated below with the job it does.

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
| 9 | **RAGAs** | Scores the finished system against generated test questions |
| 10 | **OpenWebUI** (Docker) | The chat interface the user actually types into |

### 4.11 Note on item 8

The brief breaks item 8 into three explicit sub-requirements, which is unusual and signals where
submissions typically fall short:

- **(a)** Initialize prompts *in* Arize Phoenix
- **(b)** Retrieve prompts *from* Arize when needed in the code
- **(c)** Use Arize for tracing, debugging, and observability

Only (c) is satisfied by adding a trace exporter. Requirements (a) and (b) mean prompts must
genuinely live in Phoenix and be fetched at runtime — the same point `notes.txt` makes as *"ensure
prompts can be externalized"*.

---

## 5. Decisions left to us

The brief explicitly leaves ten choices open and states they *"must be chosen by the candidate"*.
These are the graded design surface: each needs a decision and a recorded justification.

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

## 7. Reading of the assignment

This is a capability assessment presented as a product request. The chatbot itself is a
well-understood thing to build; what is being examined is whether ten prescribed enterprise tools can
be integrated coherently, and whether the open design choices can be defended.

Two consequences follow, and they shape everything downstream:

- **Coverage is graded.** A brilliant system that omits conversation memory scores worse than a
  modest one that addresses all ten baseline items. §8 exists to make omission impossible.
- **Justification is a deliverable, not a courtesy.** Working code with no recorded reasoning
  answers only half of what was asked.

### 7.1 Scope position

The client's own documents have not yet been supplied. Rather than treat that as a blocker, the
system is built **corpus-agnostic**: the engine holds no knowledge of any document domain, and
pointing it at a new document set is a configuration change. Development proceeds against a stand-in
corpus, and the client's documents drop in when they arrive.

This is a deliberate widening of scope beyond the brief's literal ask, on the grounds that a
framework which works for any document set is more valuable than one wired to a single one — and
that the brief itself never names a domain.

---

## 8. Requirements traceability

Every requirement above, mapped to where it is addressed. Status is maintained as the build
progresses; nothing is marked complete without a corresponding artefact.

| Req | Requirement | Addressed by | Status |
|---|---|---|---|
| 2.1 | Grounded answers | Verifier agent; retrieval score floor | ☐ Not started |
| 2.2 | Traceable sources | Citation assembly from chunk metadata | ☐ Not started |
| 2.3 | Conversation memory | Memory store + follow-up query rewriting | ☐ Not started |
| 2.4 | Multi-part questions | Planner decomposition into sub-questions | ☐ Not started |
| 2.5 | Stepwise reasoning | Sufficiency judgement + bounded retry loop | ☐ Not started |
| 2.6 | Ingestion workflow | Parse → chunk → contextualise → embed → index | ☐ Not started |
| 3.1 | 100% open source | Ollama-hosted models; self-hosted services | ☐ Not started |
| 3.2 | Observable | Phoenix tracing across all inference calls | ☐ Not started |
| 3.3 | Externalised prompts | Phoenix prompt registry, fetched at runtime | ☐ Not started |
| 3.4 | Measured quality | RAGAs evaluation with generated test set | ☐ Not started |
| 4.1 | Docling | Ingestion — parse stage | ☐ Not started |
| 4.2 | LlamaIndex + PGVector | Ingestion — index stage; retrieval layer | ☐ Not started |
| 4.3 | Contextual Agentic RAG | Ingestion — contextual augmentation; re-ranking | ☐ Not started |
| 4.4 | Conversation memory | *(see 2.3)* | ☐ Not started |
| 4.5 | Citation handling | *(see 2.2)* | ☐ Not started |
| 4.6 | Ollama | Model hosting | ☐ Not started |
| 4.7 | Crew.AI | Agent orchestration | ☐ Not started |
| 4.8a | Initialize prompts in Phoenix | Startup prompt registration | ☐ Not started |
| 4.8b | Retrieve prompts from Phoenix | Runtime prompt resolution | ☐ Not started |
| 4.8c | Tracing and debugging | *(see 3.2)* | ☐ Not started |
| 4.9 | RAGAs | *(see 3.4)* | ☐ Not started |
| 4.10 | OpenWebUI via Docker | Frontend service; backend API integration | ☐ Not started |
| 5.1–5.10 | Ten open design choices | One ADR each in `doc/adr/` | ☐ Not started |
| 6.1 | GitHub repo on `master` | Repository setup | ☐ Not started |
| 6.2 | README with deployment | Root `README.md` | ☐ Not started |
| 6.3 | `/doc` supporting material | This directory | ◐ In progress |
