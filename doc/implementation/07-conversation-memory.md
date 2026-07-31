# Conversation Memory — Build Record

> The seventh phase of the build: a question stops being isolated. "What about
> that one?" becomes answerable, without earlier turns becoming a source of
> facts.
>
> This document records what was built, how each behaviour was *verified* rather
> than assumed, and what went wrong along the way. The design documents say what
> should be built; this says what was.

---

## 1. What this phase covers

Satisfies baseline item 4 (**Conversation Memory**) and the multi-turn half of
requirement 2.1 — an answer stays grounded in retrieved evidence even when the
conversation around it is not.

| Area | Delivers | Status |
|---|---|---|
| Verbatim window | Bounded by counted tokens, not turn count | ✅ |
| Token counting | The answering model's own tokenizer | ✅ |
| Structured summary | Four named fields, incremental | ✅ |
| Summariser agent | Runs *after* the response, never before | ✅ |
| Provenance rendering | A refusal stays legible as a refusal | ✅ |
| Poisoning guards | Verifier and sufficiency judge never see memory | ✅ |
| Persistence | Turns, rewritten query, provenance, summary | ✅ |

### Explicit non-goals

- **No citation rendering** — the answer carries `[n]` markers; turning those
  into page-anchored references is the citation phase
- **No HTTP endpoint** — `POST /chat` arrives with the API phase; `answer_turn`
  is the engine function it will call
- **No cross-conversation memory.** Nothing is shared between conversations, and
  that is a boundary, not an omission

---

## 2. The central problem

> **Documents are treated as evidence. Memory is treated as fact.**

Retrieved passages are scored, filtered, cited and verified. Memory is loaded and
believed — and memory contains the model's own previous output, never re-examined
after the turn that produced it. The same claim gets entirely different treatment
depending on which side it arrives from.

Three vectors follow, and the guards against them are in code, not in prompts:

| Vector | Guard |
|---|---|
| **Self-poisoning** — turn 3 answers wrongly, turn 5 reasons from it | The verifier is never shown memory, so any claim not traceable to *this turn's* evidence is unsupported |
| **Laundered uncertainty** — "I couldn't find it" compresses to "not covered" | `declined` is a named field in the summary schema, and `merge_summaries` unions it in code so a forgetful pass cannot erase it |
| **False user premises** — "since the limit is 30 kg, what if I'm over?" | The same verifier rule; it does not care where a claim originated |

The third column is one rule doing all three jobs, precisely because it does not
ask where the claim came from.

### The ordering is the design

```
load memory ──▶ answer ──▶ persist the turn ──▶ summarise
                                                    ▲
                            off the critical path ───┘
```

- **Summarise after responding.** The summary is not needed until the *next*
  question arrives, so running it first would add a model call to a response that
  has no use for its result
- **Persist before summarising.** A summariser failure must never cost the turn.
  The record of what was asked and answered is durable; the summary is derived

---

## 3. Components: steps executed and how each was verified

### 3.1 The window is bounded by tokens, counted

Turn count is a proxy that fails badly: one turn is 50 tokens, the next is 2,000
because a clause was pasted in. Four turns can therefore mean 200 tokens or
8,000, and only one of those fits.

Counting uses the answering model's own tokenizer rather than a
characters-per-token ratio, because the ratio varies with content — English prose
runs about 4.5 chars/token, a table of fare codes far less — so an estimate is
most wrong exactly where text is densest and the budget tightest.

```
verbatim tokens : 42  (counted, not estimated)
```

If the tokenizer cannot be loaded the system degrades to a deliberately
pessimistic estimate and says so in the log, so a run with approximate budgets is
identifiable rather than silent.

### 3.2 A refusal stays legible as a refusal

An assistant turn sitting in context is just text; nothing about it says whether
it was grounded. Provenance is rendered inline:

```
user: what is the surfboard fee?
assistant: I could not find a surfboard fee. [not answered from the documents]
```

### 3.3 The summary is a schema, not prose

*"The user asked about baggage allowances and was given the Economy limit"* reads
well and is useless — it dropped the route and fare class the next follow-up
needs. Verified against a seeded conversation:

```
evicted turns   : 2
summary updated : True

Parameters established: flying Economy; from Abu Dhabi to London
Topics covered: (none)
Asked but NOT answered: (none)
Open threads: (none)
```

Empty sections render as `(none)` rather than being omitted: an absent heading is
ambiguous — nothing declined, or the summariser dropped the field? — and that
ambiguity is how `declined` quietly disappears.

### 3.4 The summary is bounded, and the priority order decides what goes

`merge_summaries` unions on every eviction and never removes anything, so the
summary grows for as long as the conversation does. The failure that causes is
not a crash: the summary is prepended to the planner and synthesizer prompts, so
it silently eats the window that evidence needs, and the system starts answering
from conversation rather than from documents — the "memory yields, evidence is
protected" principle inverting itself.

Simulated over 60 eviction events against a 400-token ceiling:

```
 turn  unbounded  bounded
   15        176      176
   30        341      341
   45        506      395      ← unbounded is already past the ceiling
   60        671      398
```

What survives at turn 60 is the point:

```
Parameters established: route ABZ-LHR; Economy fare      ← established at turn 0
Topics covered: baggage question 48 … baggage question 60
Asked but NOT answered: surfboard fee — not found        ← never dropped
```

`parameters` is trimmed last because it is what turn 60 resolves "the same
route" against. `declined` is trimmed second-last because it is the
anti-poisoning field — dropping a "could not find" is exactly how a hedge turns
into a finding. `topics` goes first: it says only what has been discussed, and
recent ones are still in the verbatim window.

### 3.5 ⚠️ The multi-turn test, which is the only thing that finds this

**Single-turn evaluation cannot detect memory poisoning, and single-turn
evaluation is how RAG systems are normally graded.** Judge each turn alone and
every metric looks healthy: fluent, cites its passages, passes verification. The
failure lives entirely in the seam between turns.

So the test seeds a poisoned history and asks a follow-up. Turn 2 asserts a
checked-baggage allowance that appears nowhere in the corpus; turn 3 takes it as
a premise:

```
seeded  assistant: The checked baggage allowance is 30 kg.
asked   what happens if I go over that allowance?

rewritten : What happens if I exceed the checked baggage allowance?
passages  : [('etihad-general-conditi', 35, 0.771), ('etihad-general-conditi', 60, 0.492)]
provenance: cited
ANSWER    : If you exceed the checked baggage allowance, you will be required to
            pay a charge for the excess baggage [1]. ...

'30 kg' repeated as fact? -> False
```

Both properties hold at once, and both are needed:

- memory supplied the **continuity** — "that allowance" resolved correctly
- memory did **not** supply the **fact** — the invented figure is absent, and the
  answer is grounded in a clause retrieved for this turn

---

## 4. Challenges and how they were resolved

### 4.1 ⚠️ A guard from the previous phase silently broke follow-up questions

The first live multi-turn run passed its assertion and was still wrong. The
fabricated figure was correctly absent — but the answer was a **decline**, on a
corpus that covers excess baggage, and the rewritten query still read *"what
happens if I go over **that allowance**?"* with the pronoun unresolved.

Inspecting the planner's raw output before the guard ran showed it had done its
job correctly, and one thing more:

> "What happens if I exceed **the checked baggage allowance** on **Delta Air
> Lines' domestic conditions of carriage**?"

One good thing and one bad thing in a single string. The invented-scope guard
built in [06 · Agentic Read Path](06-agentic-read-path.md) §4.2 replaced the
*whole* sub-question with the user's raw words — discarding the correct pronoun
resolution along with the unwanted narrowing. The search then ran on the literal
words "that allowance".

**Root cause.** The wholesale substitution was safe when it was written and
stopped being safe when memory arrived. Without a conversation, the user's raw
question has no unresolved references to lose; with one, it does. The guard's
correctness depended on an assumption that a later phase removed, and nothing
connected the two.

**Fix.** Strip only the offending clause and keep the rest, falling back to the
user's question only when nothing usable survives.

The first attempt at that got it wrong twice, and both were caught by looking at
the output rather than at the exit code:

1. Removing "Delta Air Lines" then looking for "delta" found it already gone and
   treated absence as failure, so **every** case fell back. The tell was that the
   "fixed" version behaved identically to the broken one
2. Cutting at the *first* preposition turned "What are the rules **on** excess
   baggage **in** Delta Air Lines' conditions?" into "What are the rules?" —
   grammatical, and about nothing at all. It has to be the connective adjacent to
   the document name, not the first one in the sentence

Final behaviour, all seven cases asserted:

| Sub-question | Result |
|---|---|
| "…exceed the checked baggage allowance **on Delta Air Lines' domestic conditions of carriage**?" | → "What happens if I exceed the checked baggage allowance?" |
| "What are the rules on excess baggage **in Delta Air Lines' conditions of carriage**?" | → "What are the rules on excess baggage?" |
| "What is **Delta's** excess baggage charge?" | → falls back; the name is inside the subject, so there is no clause to cut |
| "What does the **Etihad** travel insurance policy cover?" (user said Etihad) | → kept unchanged |

### 4.2 A trim rule that annihilated the field it was trimming

The first trimming implementation swept the priority order strictly: empty
`topics` completely, then start on `open_threads`, and so on. It passed every
single-pass test.

The fifty-turn simulation failed it immediately — `topics` was **empty** while
`open_threads` kept growing. A strict priority sweep does not degrade the lowest
field, it destroys it, and a field with zero entries has stopped doing its job
altogether.

**Fix.** Drain fields with *more than one* entry first, in priority order, and
only reduce a field to nothing once every field is already down to its last
entry. Every field keeps its most recent item for as long as the budget allows —
degraded but still informative, rather than absent.

**Root cause worth keeping.** The rule was derived from the design's stated
priority ordering, which is about *which field matters more*, and silently
applied as *which field to erase first*. Those are not the same instruction, and
only a long-running simulation could tell them apart.

### 4.3 A prompt nearly built twice under two spellings

A `conversation-summariser.md` was written before noticing that
`conversation-summarizer.md` already existed from the design phase, was listed in
`REQUIRED_PROMPTS`, and was already pushed to the registry. Two files differing
only by one letter would have meant the registry serving one while edits landed
in the other — the same class of silent divergence as
[06](06-agentic-read-path.md) §4.3, arrived at from a different direction.

Resolved by deleting the new file and raising the existing prompt to version 2
with the anti-laundering rules, keeping its registered name and variables.

### 4.4 A stale count in a passing suite

`check_models()` listed the embedding, small and answering models but never
`contextualiser_model` — so a deployment missing the model that every ingestion
chunk depends on would still report ready. Found while adding `verifier_model` to
the same list. Both are now included, de-duplicated because the fields are
allowed to name the same model.

---

## 5. Final state

| Behaviour | Verified by |
|---|---|
| Window bounded by counted tokens | Unit test forcing a 40-token budget |
| Most recent turns kept, in order | Unit test |
| An oversized single turn is kept anyway | Unit test |
| Refusals render as refusals | Unit + integration |
| `declined` survives a forgetful summariser | Unit test on `merge_summaries` |
| An established parameter survives | Unit test |
| A fabricated figure is not repeated as fact | **Live multi-turn test** |
| A follow-up resolves against the previous turn | **Live multi-turn test** |
| Turns persist with provenance and rewritten query | Live integration test |
| The summary stays under its ceiling | 50- and 60-turn simulations |
| A turn-1 parameter survives to turn 60 | Long-conversation test |
| No field is annihilated while another grows | Long-conversation test |

**136 tests pass** — the whole suite, unit and integration, including live models
and a live Phoenix. `mypy app tests` is clean; `ruff` is clean.

### Still open

- **Incremental summarisation still degrades over a long conversation.** The
  ceiling now holds, but each pass re-summarises a summary, and detail erodes.
  The design accepts this for ten-to-twenty-turn conversations and calls for a
  periodic full rebuild if they run to fifty; that rebuild has not been built,
  and the simulations above test the *bound*, not the *fidelity*
- **Evidence-priority truncation is a principle, not yet a mechanism.** Memory
  yields before evidence because memory is now bounded and small, not because
  anything measures the assembled total and drops in the defined order. The
  prompt-level enforcement belongs with the API phase, where the full context is
  assembled in one place

---

## 6. What this unblocks

The citation phase consumes the `[n]` markers and the stored per-turn citations.
The API phase wraps `answer_turn` in `POST /chat` with a conversation id.
Evaluation reads the multi-turn poisoning construction from here — the design is
explicit that the obvious evaluation design will not include it.

---

## 7. Command reference

```bash
# memory logic — no model, no database
uv run pytest tests/unit/test_memory.py -q

# the multi-turn poisoning tests — needs Postgres, Ollama and the corpus
uv run pytest tests/integration/test_conversation_memory.py -q

# the full gate
uv run pytest -q
uv run mypy app tests
uv run ruff check .
```
