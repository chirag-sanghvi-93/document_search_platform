# Conversation Memory

> Baseline item 4 of 10 — *"Conversation Memory"*.
>
> A **mechanism**, not a tool. Nothing is installed for it: storage is the existing database, and
> summarisation uses the existing language model. Every design decision here is ours, since no
> library defines correct behaviour.
>
> Satisfies requirement 2.3.

---

## 1. What it is

Retaining the dialogue so follow-up questions work.

It does **two jobs**, which are conflated constantly:

| Job | Needs | Used by |
|---|---|---|
| **Resolve references** | The last few turns, verbatim | The rewriting stage, before retrieval |
| **Inform the answer** | The gist of the whole conversation | The drafting stage, after retrieval |

*"What about business class?"* needs job 1 — exact recent wording, so the reference resolves.
*"You mentioned an exclusion earlier"* needs job 2 — the shape of the conversation, not its
transcript.

One store, two consumers, different requirements from each.

### The central tension

**Conversation history and retrieved evidence compete for the same finite context.**

A long conversation quietly pushes out the passages an answer depends on. Nothing errors — the system
simply begins answering from memory instead of from documents, which is exactly the failure
requirement 2.1 forbids, arriving through a side door.

| Approach | Problem |
|---|---|
| Keep everything | Breaks on long conversations |
| Last N turns only | Loses early context that follow-ups routinely reference |
| **Rolling summary + last N verbatim** | More moving parts — and the only one that survives both |

The third is adopted: recent turns kept exactly, older ones compressed into a running summary.

---

## 2. Use-cases covered

| # | Use-case | Purpose |
|---|---|---|
| 1 | Store each turn | The record itself |
| 2 | Load recent turns verbatim | Feeds reference resolution |
| 3 | Maintain a rolling summary | Older context without the token cost |
| 4 | Detect when summarisation is due | Triggered by token budget |
| 5 | Isolate by conversation | One conversation's history never reaches another |
| 6 | Expire old conversations | Bounded growth, and a retention position |

---

## 3. Workflow

```
Question arrives with a conversation id
   │
1  Load memory          summary + last N turns          [#2, #3]
   │
2  Rewrite follow-up    using recent turns only         [#2]
   │
   ├──────────► retrieval and the reasoning loop
   │
3  Draft answer         summary + evidence              [#3]
   │
4  Append the turn      question, answer, provenance    [#1]
   │
5  Over budget?         update the summary              [#4]
```

**Step 2 uses recent turns only, never the summary.** A summary saying *"the user asked about baggage
allowances"* is useless for resolving *"what about business class?"* — the specific route and fare
class have been compressed away. Reference resolution needs the actual words.

**Step 3 receives the summary but not the recent turns.** The planner has already folded those into
the rewritten standalone question, so passing them again duplicates context that evidence needs.

**Step 5 happens after responding.** See §4.

---

## 4. Summarisation strategy

### Trigger

**Token budget, not turn count.** Turn count is a proxy that fails badly: one turn is 50 tokens, the
next is 2,000 because a clause was pasted in. The resource actually running out is context, so it is
measured directly.

### Scope

Only the turns **falling out of the verbatim window** are summarised — not the whole conversation
each time.

```
[ summary ][ turn 4 ][ turn 5 ][ turn 6 ][ turn 7 ]
              ↑
        evicted next → folded into summary
```

So `new_summary = f(old_summary, evicted_turns)`. The summary absorbs turns as they age out.

### Incremental, not from scratch

| | Incremental | From scratch |
|---|---|---|
| Cost | Only the new evictions | Grows with conversation length |
| Quality | Degrades — each pass re-summarises a summary | Consistent |
| Ceiling | None | Eventually the full history will not fit |

The degradation is real: it is a telephone game, and detail erodes with each pass. Incremental is
chosen on the grounds that document-search conversations are short — ten to twenty turns means two or
three summarisation events, over which drift is negligible. If conversations turn out to run to
fifty turns, revisit with a periodic full rebuild.

### Format — structured, not prose

This is where a generic summary fails. *"The user asked about baggage allowances and was given the
Economy limit"* reads well and is useless: it has dropped the route and fare class the next follow-up
needs.

```
Parameters established:   route ABZ–LHR, Economy fare, Etihad Guest Gold
Topics covered:           checked baggage allowance, sports equipment fees
Declined / unanswered:    surfboard fee — not found in the documents
Open threads:             user has not yet been answered on excess charges
```

Two reasons a schema beats prose:

- **It resists drift.** A schema instructs each pass what to keep. Prose gives no such instruction,
  so each pass discards whatever it happens to find least interesting.
- **It is inspectable.** When a follow-up resolves wrongly, it is immediately visible whether the
  parameter was lost or misused.

The `Declined / unanswered` field is not cosmetic — see §6, vector 2.

Dropped from the summary: verbatim phrasing, citations (stored per turn), pleasantries.

### Timing — after responding

Summarisation is one model call per event, cheap against the four to seven an answer costs. But run
synchronously it lands **on the critical path**, adding seconds to an already slow response.

It does not need to be there. The summary is not required until the *next* question arrives. Answer
first, then summarise.

### Failure modes

| Failure | Guard |
|---|---|
| Summary drops a parameter a follow-up needs | The schema — parameters are a named field, not something judged interesting |
| Summary grows unbounded | Cap its length explicitly. A compression step that is not capped is not compressing |
| Summarisation fails or times out | Retain more turns verbatim. Degrade, do not fail |
| Summary absorbs a wrong answer as fact | See §6 |

---

## 5. Context budget

### What competes for the window

| Consumer | Notes |
|---|---|
| Instructions | The stage's prompt |
| Conversation memory | Summary + verbatim turns |
| Retrieved evidence | The passages |
| The question | Small |
| **Room to generate the answer** | Input and output share the window |

The last row is routinely forgotten. Fill the window with input and there is nowhere for the answer
to go — generation truncates mid-sentence.

### The protection principle

**Evidence is protected. Memory yields.**

Evidence is what the answer is meant to be based on. If memory crowds it out, the system answers from
conversation rather than documents — silently, with no error raised.

### Evidence size is already fixed

```
chunk size × chunks kept  =  evidence budget
   768     ×      5       ≈  3,840 tokens     quality profile
   768     ×      3       ≈  2,304 tokens     fast profile
```

Both numbers were chosen for retrieval quality and have quietly set the context budget as a side
effect. If evidence must be smaller, one of those two moves; there is no third lever — which is
exactly what the fast profile does.

### Per-stage budgets — where the real saving is

There is no single budget. **There is one per stage, and most stages need far less than everything.**

| Stage | Needs | Does not need |
|---|---|---|
| Classify | Question + recent turns | Evidence |
| Rewrite | Question + recent turns | Evidence, summary |
| Judge sufficiency | Question + candidates | Memory entirely |
| Draft | Question + evidence + summary | — |
| Verify | Draft + evidence | Memory, and the question's framing |

The instinct is to assemble one context and pass it everywhere. That wastes most of the window on
most calls, and actively harms two stages: the verifier should not see the reasoning that produced
the draft, and sufficiency judging should not see conversation history that can make weak evidence
look adequate.

**Pass each stage only what it needs** — cheaper, and more correct.

### Enforcement

Count tokens before sending, using the model's own tokenizer. Not estimate — count.

When over budget, drop in a defined order:

```
1. Oldest verbatim turns      → folded into summary
2. Summary detail              (already capped)
3. Lowest-scoring evidence     ← last resort
   ─────────────────────────
   Never: instructions, the question
```

Evidence dropping must be *defined* even though it is last, because otherwise it happens anyway —
arbitrarily, at the serving layer.

### ⚠️ The silent failure

Exceed the window and most serving layers truncate **from the start**, removing the instructions
first. The model then behaves as though it was never told what to do: no error, no warning, just an
answer that ignores every rule set for it.

This is the most likely way the system misbehaves without anyone understanding why. Explicit token
counting is the only guard.

### Hardware interaction

Context size is not free locally. It is a configuration setting, and a larger window means a larger
key-value cache held in memory — a real constraint on a machine shared with the database, tracing,
and the interface.

The budget is therefore squeezed from both ends: the window cannot simply be enlarged to make room,
and per-stage trimming is the only lever that costs no memory.

---

## 6. The poisoning problem

### The asymmetry at the root of it

> **Documents are treated as evidence. Memory is treated as fact.**

Retrieved passages are scored, filtered, cited, and verified. Memory is loaded and believed. But
memory contains **the model's own previous output**, never re-examined after the turn that produced
it.

The same claim therefore receives entirely different treatment depending on where it arrives from.

Memory is a record of *what was said*, not *what is true*.

### Three vectors

**1 · Self-poisoning.** Turn 3 answers *"the allowance is 30 kg"* — wrong. Turn 5 asks a follow-up,
the model sees 30 kg in context and reasons from it. The error is now load-bearing, and nothing
revisits it.

**2 · Summarisation launders uncertainty into assertion.** ⚠️ The nastiest.

```
Turn 3 said     "I couldn't find a surfboard fee in these documents."
Summary says    "Surfboard fee: not covered."
```

A hedge has become a finding. Compression strips qualifiers first — they are the least
information-dense part of a sentence, so any summariser discards them — and what remains reads as
established fact.

**3 · User-asserted premises.** *"Since the limit is 30 kg, what happens if I'm over?"* The false
premise enters memory as though established.

### The structural guard

**The verifier does not accept memory as a source.**

Every factual claim in an answer must trace to evidence **retrieved for that turn**. Memory may set
the topic, resolve a reference, or supply continuity. It may not supply a fact.

If a claim appears in the draft and is not in this turn's evidence, it is unsupported — regardless of
whether it originated with the model, with memory, or with the user's own phrasing. One rule covers
all three vectors precisely because it does not care where the claim came from.

The cost is that some correct claims are re-retrieved needlessly. Cheap, and the right trade.

### Supporting guards

| Guard | Against |
|---|---|
| Tag provenance per turn — answered with citations, hedged, or declined | Vectors 1 and 2 |
| Explicit `Declined / unanswered` field in the summary schema | Vector 2 — uncertainty gets a named slot instead of being compressed away |
| Reference resolution leans on user turns over assistant turns | Vector 1 |
| Memory excluded from sufficiency judging | Prevents history making weak evidence look adequate |

The second is why the structured summary matters beyond tidiness: a prose summariser has no
obligation to preserve *"could not find"*; a schema with a named field does.

### Detection

Single-turn testing will not find this, and single-turn testing is how RAG systems are normally
evaluated. Every metric looks healthy, because each turn judged alone is well grounded.

**A multi-turn test is required:** seed a conversation in which turn 3 receives deliberately wrong
context, then check whether turn 5 propagates it. Straightforward to construct, and the only thing
that detects this class of failure.

Recorded here as a requirement on the evaluation work, because the obvious evaluation design will
not include it.

---

## 7. What gets stored

Per turn: conversation id, turn index, role, content, timestamp — plus two added deliberately:

| Field | Why |
|---|---|
| **The rewritten query**, alongside the original | Makes debugging possible when a follow-up resolves wrongly. Invisible otherwise |
| **Citation references**, not retrieved text | The content already lives in the index; duplicating it into memory bloats every subsequent load |
| **Provenance** — cited / hedged / declined | Feeds the summary schema and the guards in §6 |

---

## 8. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Retention approach | Rolling summary + last N turns verbatim | The only approach surviving both long conversations and early references |
| Reference resolution input | Recent turns only, never the summary | The summary has compressed away the specifics it needs |
| Summarisation trigger | Token budget | Turn count is a proxy that fails on uneven turn sizes |
| Summarisation scope | Only turns being evicted | Not the whole conversation each time |
| Summarisation method | Incremental | Revisit with periodic rebuild if conversations run long |
| Summary format | **Structured fields, not prose** | A schema resists drift and is inspectable |
| Summary length | Explicitly capped | An uncapped compression step is not compressing |
| Summarisation timing | After responding | Not required until the next question; keeps it off the critical path |
| Summarisation failure | Retain more verbatim turns | Degrade, do not fail |
| Context priority | Evidence protected, memory yields | Otherwise the system answers from conversation, silently |
| Generation headroom | Explicitly reserved | Input and output share the window |
| Budget scope | Per stage, not one shared context | Cheaper, and more correct for the verifier and the judge |
| Memory in sufficiency judging | Excluded | It can make weak evidence look adequate |
| Budget enforcement | Token counting with the model's tokenizer | Truncation otherwise removes the instructions first, silently |
| Overflow order | Verbatim turns → summary detail → lowest-scoring evidence | Instructions and question never dropped |
| Memory as a factual source | **Never** | Claims must trace to evidence retrieved this turn |
| Verifier scope | Retrieved evidence only | Not memory, not the reasoning that produced the draft |
| Turn provenance | Recorded — cited / hedged / declined | |
| Stored per turn | Rewritten query and citation references, not retrieved text | |
| Evaluation | Multi-turn propagation test required | Single-turn metrics cannot detect poisoning |

### Still open

| Item | Decided under | Settled by |
|---|---|---|
| Verbatim window size (N turns) | This document | Calibration against typical conversation length |
| Token budget split between summary and verbatim turns | This document | Measurement |
| Which model performs summarisation | Ollama — design choice 5.5 | Evaluation |
| Summarisation prompt wording | Arize Phoenix — design choice 5.8 | Evaluation |
| Conversation retention period | This document | Not yet discussed — has a privacy dimension |
