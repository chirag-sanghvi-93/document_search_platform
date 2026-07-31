# Open Items

> Every known-open item from the build records, in one place.
>
> Each is recorded where it was found, in that phase's record. This page exists
> so that they can be read as one list rather than reconstructed from six
> documents — and so that nothing is quietly carried as "we'll get to it".

An item is on this page because it is **known, deliberate, and not blocking**.
Anything blocking was fixed in the phase that found it.

---

## Waiting on a later phase

These are not unresolved questions. The mechanism they need does not exist yet,
and building it early would mean building it twice.

| Item | Found in | Resolved by |
|---|---|---|
| **Reasoning-content and `<details>` rendering unverified in the interface.** Confirmed over SSE with `curl`; not yet seen in the pinned v0.6.5 image | [09](09-api-frontend.md) | Eyes on the running frontend |
| **Evidence-priority truncation is a principle, not a mechanism.** Memory yields to evidence because memory is bounded and small, not because anything measures the assembled total and drops in the defined order | [07](07-conversation-memory.md), [09](09-api-frontend.md) | Still open — deferred to the API phase and not built there |
| **`keep_floor = 0.3` is the design's provisional value**, never fitted | [05](05-retrieval.md), [06](06-agentic-read-path.md) | Evaluation phase — calibration split, never the evaluation split |
| **Under-citation is not measured.** The proportion of factual sentences carrying a marker is a health metric the design asks for; only the invalid-marker count exists so far | [08](08-citations.md) | Evaluation phase |
| **Deep linking (`document.pdf#page=14`) is not built** | [08](08-citations.md) | Needs a decision on whether the client's documents are servable — not a technical gap |

## Accepted trade-offs

Measured, understood, and deliberately not pursued further.

| Item | Found in | Why it stands |
|---|---|---|
| **The `fast` profile is weak on comparisons.** With 3 passages split across two subjects, each side gets one — and one passage cannot characterise a policy. Observed live: the single Etihad passage that fitted was its glossary page | [09](09-api-frontend.md) | **This is what the profile is for.** `fast` trades evidence for latency; `agentic-rag` (5 passages) handles comparisons correctly. Scaling the budget by subject count was considered and rejected — it would erase the distinction between the two profiles |
| **26–34 s for a lookup.** Down from 553 s, but still slow | [06](06-agentic-read-path.md) | The remaining cost is three sequential model calls on local CPU/Metal hardware. Further gain needs a structural change — streaming, or running planner and retrieval concurrently — not another parameter. Revisit after the API phase, where streaming becomes available |
| **The comparison case at 93.7 s** sits at the top of the 30–90 s target | [06](06-agentic-read-path.md) | 6 model calls and 5 searches; not obviously wrong, but the case to watch |
| **`ef_search = 100` is not yet distinguishable from 40** | [05](05-retrieval.md) | The corpus is too small for the difference to show. Re-measure at ~10× size |

## Known limitations

Real gaps in what was built, with the failure they permit stated plainly.

| Item | Found in | What it permits |
|---|---|---|
| ⚠️ **Lowercase qualifiers are not caught by the invented-scope guard.** It catches organisation and document names ("Delta Air Lines"), not "domestic" or "per kilogram" | [06](06-agentic-read-path.md), [09](09-api-frontend.md) | **Observed costing a correct answer live**: "what is the excess baggage charge?" was searched as "the rate for excess baggage per kilogram", and the clause that answers it fell below the floor and came back as a *near miss* instead of a source |
| **Incremental summarisation erodes detail.** The token ceiling holds across 60 simulated evictions, but each pass re-summarises a summary | [07](07-conversation-memory.md) | Over a conversation of ~50 turns, older context degrades. The design calls for a periodic full rebuild at that length; it has not been built. The simulations test the *bound*, not the *fidelity* |
| **The two-markers-per-claim cap is prompt-only.** Nothing enforces it in code | [08](08-citations.md) | A model that appends four markers to one sentence produces four; it looks thorough and tells the reader nothing |
| **Preambles open with "This passage…"**, which the contextualiser prompt forbids | [04](04-ingestion.md), [05](05-retrieval.md) | Cosmetic. `display_text` is what gets cited, so the wording never reaches a user or a citation |

---

## What is deliberately not here

Design rationale belongs in [`../components/`](../components/), and the argued
alternatives belong in [`../adr/`](../adr/). An item that turns out to need a
decision rather than a fix moves there and is struck from this page.
