# ADR 0001 · Crew.AI owns the agent roles; the application owns control flow

| | |
|---|---|
| **Status** | Accepted |
| **Baseline item** | 7 — Crew.AI |
| **Specified by** | [07 · Crew.AI](../components/07-crewai.md) |
| **Recorded in** | [09 · API & Frontend](../implementation/09-api-frontend.md) |

---

## Context

The read path has four roles — planner, retrieval specialist, synthesizer,
verifier — a **loop** (retry until the evidence is sufficient) and **branches**
(short-circuit when a question needs no retrieval; fan out across sub-questions).

An agent framework can express some of that and not the rest. The design document
settled the split in §4; this ADR records what was actually built, because for a
period **it did not match**.

### What went wrong first

The four roles were implemented as plain async functions posting to Ollama, with
no framework involved. `crewai` sat in `pyproject.toml`, uninstalled in the image
and unimported in the code, while docstrings across the read path pointed at
`doc/components/07-crewai.md` as "the authority on read-path control flow" — so
the code read as though the framework were in use. Naming reinforced it:
`agents.py`, `AgentOutcome`, `AgentSettings`.

That was drift, not a decision. It was found by asking the question directly, not
by any test — nothing could have failed, because a dependency that is never
imported breaks nothing.

## Decision

**Use Crew.AI for the agent roles. Keep control flow in the application.**

| Owner | What |
|---|---|
| Application | The out-of-scope and capability short-circuits, the fan-out across sub-questions, the shared search budget, deduplication |
| Crew.AI agent | Its own retry loop, bounded by `max_iter`, calling one tool repeatedly until the evidence is sufficient |

Four agents, never three: an agent carries its own context, so a shared
synthesizer/verifier would let the verifier see the reasoning that produced the
draft — and a model tends to accept a claim it has just justified.

One tool, never four: tool-selection error is the dominant failure mode at this
model size.

`allow_delegation=False` everywhere. Hierarchical delegation was rejected in
design because it depends on routing reliability an 8B-class model does not have.

### What deliberately did NOT move into the framework

Parsing, the invented-scope guard, the `display_text`-only rule, the citation
merge and every degradation path stay in application code and are applied to the
crew's output exactly as to a direct call.

This is why the framework could be adopted without re-verifying every property
from scratch: **Crew.AI changes who talks to the model, not what is trusted about
the reply.**

## Both paths are kept, and the flag is not indecision

`agents.use_crewai` selects between the crew path and direct model calls. Both
implement the same four roles and the same split of control flow.

The framework's cost is real: role, goal, backstory and task scaffolding are
prepended to every call and compete for the same 8192-token window that must also
hold instructions, evidence, memory and generation headroom. Measured against the
same corpus and questions:

| Question | Crew.AI | Direct |
|---|---|---|
| "what is the excess baggage charge?" | 4 calls · cited · **60.0 s** | 3 calls · cited · **35.5 s** |
| "am I covered if my baggage is lost?" | 4 calls · cited · **66.5 s** | 3 calls · hedged · **54.5 s** |
| "how do I bake sourdough bread?" | 1 call · declined · 2.3 s | 1 call · declined · 2.2 s |

Roughly **1.2–1.7× slower on answered questions, identical on short-circuited
ones** — which is the expected shape, since a short-circuit never reaches an
agent on either path.

Keeping the direct path callable is what makes that cost measurable rather than
assumed, and it is what the evaluation ablation compares. The design document
already anticipates configurable paths for exactly this reason.

## Consequences

**Good**

- Baseline item 7 is genuinely satisfied, not merely declared
- The retry loop is now a configured `max_iter` rather than hand-written counting
- The framework's overhead is a number, not an opinion

**Bad**

- Every answered question is slower, on a path already flagged as slow
- One more heavyweight dependency in the image — and its absence made every chat
  request 500 until a readiness probe was added for it
- Crew.AI's own telemetry had to be opted out of. The first attempt used
  `OTEL_SDK_DISABLED`, which silenced OpenTelemetry process-wide and disabled
  **our** Phoenix tracing — the whole of baseline item 8. Two live tracing tests
  were the only sign

**Neutral**

- Two code paths to keep working. `test_crew_control_flow.py` asserts the control
  flow is identical on both, so a branch cannot silently migrate into the
  framework
