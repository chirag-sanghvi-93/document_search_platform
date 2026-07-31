# Crew.AI

> Baseline item 7 of 10 — *"Crew.AI — Agentic orchestration framework"*.
>
> **This document is the authority on the read path's control flow.** Other components describe
> stages; this one arranges them.
>
> Serves requirements 2.4 and 2.5. Design choice 5.8 (agent prompts) is decided here in shape, and
> the prompt text itself lives in the prompt registry per requirement 3.3.

---

## 1. What it is

A framework for orchestrating several language-model agents working together.

| Concept | What it is |
|---|---|
| **Agent** | A role with a goal — in practice a structured system prompt, plus the tools it may call |
| **Task** | A unit of work with a description and an expected output, assigned to an agent |
| **Tool** | A function an agent can invoke |
| **Crew** | Agents and tasks together, run under a **process** |

### Scope

> **It owns the control flow of the read path** — what happens, in what order, and what gets called.

It does not retrieve, store, or host models. It sequences. Everything other components describe as a
*stage* is what this arranges.

---

## 2. The four agents

| Agent | Owns | Tools |
|---|---|---|
| **Planner** | Classify, rewrite follow-ups, decompose | None — pure reasoning |
| **Retrieval specialist** | Search, judge sufficiency, retry | The search tool |
| **Synthesizer** | Draft the answer with markers | None |
| **Verifier** | Check claims against evidence | None |

Three would be tempting — synthesis and verification look like one job. **They cannot share an
agent**, because the verifier must not see the reasoning that produced the draft. An agent carries
its own context, so combining them means the checker sees the argument it is meant to check
independently, and models tend to accept a claim they have just justified.

That constraint fixes the agent count at four.

---

## 3. Agent and task design

### One task per agent, producing structured output

The tempting shape is a task per *decision*: classify, then rewrite, then decompose — three tasks,
three calls.

**One task.** They are interdependent (rewriting must precede decomposition), and one structured
output costs one call rather than three. On a path already running tens of seconds, two fewer round
trips per question outweighs the tidiness of separate tasks.

### Interfaces

**Planner**

```
IN     question + recent turns (verbatim)
OUT    { intent:              lookup | comparison | summary | out_of_scope,
         standalone_question: "...",
         sub_questions:       ["...", "..."] }        1–4
TOOLS  none
```

No evidence — nothing has been retrieved yet. No conversation summary either: reference resolution
needs the actual recent wording, which the summary has compressed away.

**Retrieval specialist**

```
IN     one sub-question + filters
OUT    { passages:   [...],          the kept ones
         sufficient: true | false,
         attempts:   2 }
TOOLS  search(query, filters)
MAX    3 iterations — initial plus two retries
```

**No conversation memory** — history can make weak evidence look adequate.

Invoked once per sub-question, so this is the only agent that runs more than once.

**Synthesizer**

```
IN     standalone question + numbered passages (display_text) + memory summary
OUT    draft answer with [n] markers
TOOLS  none
```

**Verifier** — runs on the small model; see the model-selection document

```
IN     the draft + the numbered passages
OUT    { revised_answer, retracted_claims }
TOOLS  none
```

**Not the question, and not the reasoning.** Groundedness is *"is this claim in the evidence"* — the
question is irrelevant to it, and withholding it prevents the verifier reasoning *"well, they asked
about X, so this is fair enough."*

### The orchestration

The application owns branching and fan-out:

```
plan = planner(question, recent_turns, corpus_description)

if plan.intent == out_of_scope:
    return short_circuit                    ← no retrieval, no further calls

results  = [retrieval(sq) for sq in plan.sub_questions]
passages = dedupe_and_number(results)
draft    = synthesizer(plan.standalone_question, passages, summary)
final    = verifier(draft, passages)
```

The framework governs what happens inside those four calls. The loop that matters — retry — lives
inside the retrieval agent, not here.

**`corpus_description` is what makes out-of-scope classification informed rather than guessed.** It is
assembled from the documents' own descriptions — operator-supplied where given, generated otherwise —
and refreshed when the corpus changes rather than per request. Without it the planner is inferring
scope from the question alone, which works for *"what is the weather"* and fails for anything whose
answerability depends on what the corpus happens to contain. See
[11 · FastAPI](11-fastapi.md) §4.

### Call count

| Scenario | Calls |
|---|---|
| Simple lookup, evidence found first try | **4** |
| Two sub-questions, one retry each | **7** |
| Out of scope | **1** |

Which is where the out-of-scope short-circuit earns its place: a 4× saving on questions that were
never answerable.

### ⚠️ Cross-sub-question deduplication

**Two sub-questions can return the same chunk.** *"Compare Economy and Business allowances"*
decomposes into two searches that may both surface the same fees table. Unhandled, that chunk enters
the numbered set twice, receives two markers, and the answer cites one passage as though it were two
independent sources.

`dedupe_and_number` therefore does two things in order:

1. **Deduplicate across sub-questions** by chunk identity
2. **Then** number the surviving set

This is distinct from the citation-level deduplication specified elsewhere. That one merges by
`file + page` *after* drafting, for presentation. This one merges by chunk *before* drafting, so the
model never sees a duplicate at all. Both are needed; they use different keys at different times.

---

## 4. The control-flow problem

The read path has a **loop** (retry) and **branches** (out-of-scope short-circuit, fan-out over
sub-questions). A straightforwardly sequential process expresses neither.

| Approach | Assessment |
|---|---|
| Hierarchical — a manager agent delegates and loops | Depends on delegation reliability an 8B model does not have |
| Loop inside the agent's tool iteration | Natural fit — adopted |
| Application owns every loop | Works, but reduces the framework to prompt scaffolding |

**The split adopted:**

- **The application owns the outer control flow** — the out-of-scope short-circuit (before any crew
  runs, so it costs nothing) and the fan-out across sub-questions.
- **The retrieval agent owns its own retry loop**, calling its search tool repeatedly within one task
  until evidence is sufficient.

The retry bound becomes the agent's maximum-iteration setting: a configured limit rather than
hand-written loop counting.

### Tools — keep the surface tiny

The retrieval agent gets essentially **one** tool: `search(query, filters)`, returning ranked
passages.

Resist adding more. Every additional tool is another choice a small model can get wrong, and
tool-selection error is the dominant failure mode at this size. One tool called repeatedly with
different queries is far more reliable than four tools called once each.

---

## 5. The retry loop

```
search(sub_question)
   │
   ├─ passages + top score returned
   │
   ├─ sufficient?  ──yes──► return passages
   │
   └─ no, iterations left ──► reformulate ──► search again
                              │
                       exhausted ──► return best found, flagged insufficient
```

### The tiered signal, and a fast path

Sufficiency judging is tiered: re-rank score first (free), model judgement only in the ambiguous
band. The tool returns that signal alongside the passages:

```
{ passages: [...], top_score: 0.82, signal: "clearly_sufficient" }
```

Which raises a question worth deciding: **when the first search returns clearly sufficient evidence,
does the agent need to run at all?**

It does not. The application can call search directly, check the signal, and invoke the agent only
when the result is ambiguous or insufficient.

| | Calls for a simple lookup |
|---|---|
| Agent always runs | 4 |
| Fast path on clear results | **3** |

⚠️ The honest tension: this can be read as the happy path not being agentic. The defence is that
requirement 2.5 asks the system to *assess* whether results are good enough, and a calibrated score
is an assessment — one whose thresholds were derived from labelled data, arguably more trustworthy
than asking a model. The model assessment fires whenever the system is genuinely uncertain.

**Made configurable.** The ablation study must compare with and without agentic retry regardless, so
a flag costs nothing and settles the argument by measurement.

### Reformulation

| Strategy | When |
|---|---|
| Different vocabulary | The question's wording may not match the document's |
| Broaden — drop constraints | Over-specific queries return nothing |
| Narrow using terms from weak results | The results hint at the document's own language |

**The agent must see what it has already tried.** Without that, models reformulate to something
near-identical and burn the budget going nowhere. The tool response carries previous attempts, and
the instruction requires meaningful difference — not a synonym swap.

### Exhaustion returns evidence, not nothing

When iterations run out, the agent returns **the best passages it found**, flagged insufficient.

The distinction matters downstream. *"No evidence"* and *"weak evidence"* are different states:

- The synthesizer must know which, so it can decline rather than answer from inadequate material
- Those weak passages are exactly what populates the **"closest matches"** display specified for
  declined answers

Exhaustion therefore feeds a designed behaviour rather than producing a dead end.

### Partial sufficiency

Sufficiency is **per sub-question, not global.**

A comparison question can have one half well-evidenced and the other not. The correct response is to
answer the first part and decline the second — not to refuse the whole question, and not to present
both as equally supported.

The sufficient flag therefore travels per sub-question through to synthesis, and the synthesizer's
instructions must handle mixed input.

### ⚠️ The global budget

Per-sub-question bounds are not enough:

```
1 planner + (4 sub-questions × 3 iterations) + 1 synth + 1 verify  =  15 calls
```

Far past any acceptable response time.

**A total retrieval budget across the whole question** — around six searches, regardless of how it
decomposed. Sub-questions draw from a shared pool rather than each holding an independent allowance.

The consequence is that a later sub-question may get fewer retries because earlier ones consumed the
budget. That is correct; the alternative is a widely-decomposed question taking three minutes.

---

## 6. Reliability and fallbacks

### The root risk

An 8B model driving a framework that assumes agents reliably produce parseable output and select
tools correctly. It does both imperfectly, and every stage compounds the previous one's error rate.

### Tool-calling failures

| Failure | Guard |
|---|---|
| Wrong tool selected | Only one tool exists |
| Malformed arguments | Validate; return a structured error the agent can act on, rather than throwing |
| Hallucinated tool name | Same — reject with a usable message |
| **Never calls the tool at all** | Hard failure — see below |

If the retrieval agent returns a final answer **without ever calling search**, it has fabricated from
nothing — and its output looks perfectly well-formed. Treat "no tool call" as a hard failure, not a
valid outcome: the agent must invoke search at least once or its result is discarded.

### Structured-output failures

Not JSON, wrong shape, JSON wrapped in prose, or truncated at the token limit. Guards: enforced
structured output where the serving layer supports it, then bounded parse retry, then fail forward.

### Fail forward

Each agent needs a defined fallback, because "the request errors" is the worst available outcome:

| Agent | Fallback | Result |
|---|---|---|
| **Planner** | One sub-question, intent `lookup`, no rewriting | Retrieval still happens. Follow-ups resolve badly, but the system answers |
| **Retrieval** | Bypass the agent — one plain search | Degrades to single-shot retrieval |
| **Synthesizer** | Retry, then return retrieved passages with a note | No answer, but the evidence remains useful |
| **Verifier** | Retry, then return the draft **explicitly marked unverified** | See below |

The planner fallback is the instructive one: **failing to plan degrades into exactly the behaviour of
an ordinary retrieval pipeline**, which still answers most questions. The agentic layer is additive,
so losing it costs capability rather than availability.

### ⚠️ The verifier question

If verification fails, ship an unverified answer or refuse?

Refusing means a verifier bug takes down the whole system. Shipping silently means an unchecked claim
reaches a user beneath a page citation — the exact failure the verifier exists to prevent.

**Ship it, marked unverified, visibly.** The caveat belongs in the answer itself, not in a log line. A
user who can see *"this answer was not checked against the sources"* can decide what to do about it;
one who cannot, cannot.

Silent degradation is what to refuse, not degradation.

### The failure that looks like success

More dangerous than crashes: **agents that run correctly and decide nothing.**

| Symptom | What is actually broken |
|---|---|
| Sub-question count always 1 | Decomposition never fires — comparisons handled as lookups |
| Retries always 0 | The loop is inert; single-shot retrieval with extra latency |
| Retractions always 0 | The verifier is a rubber stamp |
| Intent always `lookup` | Classification is degenerate; out-of-scope never short-circuits |

Every one produces well-formed output, passes every error check, and appears healthy.

**Detection: record the decisions, not only the errors.** Per request, capture intent, sub-question
count, retries used, and claims retracted. Then examine the *distributions* across a run of
questions. A distribution that never varies means the agent is not deciding anything.

This is a **required health check, not an optional one.** Four of the six behaviours that make this
system agentic can fail into inertness without a single error being raised.

### Timeouts

Every agent call needs one — a hung model call otherwise blocks the request indefinitely. A global
request timeout too, past which the system returns best-effort output from whatever completed rather
than nothing.

---

## 7. Where the prompts live

Agent roles and goals are prompts. They come from the prompt registry at runtime rather than from
code — requirement 3.3 — which is what makes the crew's behaviour tunable without redeployment.

### ⚠️ Framework token overhead

Role, goal, backstory, task description and expected-output scaffolding are prepended to every call.
That competes directly with the context budget: the 8192-token window must accommodate the
framework's scaffolding as well as instructions, evidence, memory and generation headroom.

Agents also narrate their intermediate reasoning, which costs tokens and time at every step.

---

## 8. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Scope | Owns read-path control flow | Does not retrieve, store, or host models |
| Agent count | Four | Verification cannot share an agent with synthesis |
| Planner structure | One task, structured output | Three separate tasks would cost two extra calls per question |
| Planner input | Question + recent turns only | No summary — rewriting needs verbatim wording |
| Retrieval input | One sub-question; no memory | Memory can make weak evidence look adequate |
| Retrieval invocations | One per sub-question | The only agent running more than once |
| Synthesizer input | `display_text` passages, numbered, plus summary | |
| Verifier input | Draft + passages only | Not the question, not the reasoning that produced the draft |
| Process shape | Application owns branching and fan-out; agent owns its retry loop | Hierarchical delegation is unreliable at this model size |
| Tool surface | **One tool** | Removes tool-selection error entirely |
| Iteration bound | 3 per sub-question | Configured on the agent |
| **Global retrieval budget** | ~6 searches per question | Per-sub-question bounds allow a 15-call worst case |
| Fast path | Skip the agent on clearly sufficient first results | **Configurable**, so the ablation can measure it |
| Reformulation | Strategies supplied in the prompt; attempt history returned | Improvised reformulation drifts to near-identical queries |
| On exhaustion | Return best passages, flagged insufficient | Feeds the "closest matches" display |
| Sufficiency granularity | **Per sub-question** | Partial answers are correct |
| Cross-sub-question dedup | Before numbering | Distinct from citation-level merging |
| Out-of-scope short-circuit | Before any retrieval | 4 calls down to 1 |
| Invalid tool arguments | Structured error returned to the agent | It can correct; an exception cannot be corrected |
| **No tool call from retrieval agent** | Hard failure, result discarded | Well-formed output fabricated from nothing |
| Planner failure | Degrade to single sub-question lookup | Falls back to ordinary retrieval behaviour |
| Retrieval failure | Bypass the agent, single plain search | |
| Synthesizer failure | Retry, then return passages with a note | |
| Verifier failure | Return draft **marked unverified in the answer** | Refusing lets a verifier bug take down the system |
| **Decision distributions** | Recorded per request and reviewed | The only detector for agents that decide nothing |
| Timeouts | Per agent call, plus a global request timeout | Best-effort output beats no output |
| Prompt source | The prompt registry, at runtime | Requirement 3.3 |

### Answered by implementation

| Item | Answer |
|---|---|
| Structured-output reliability at this size | **Sufficient, but only with defensive parsing.** The models wrap JSON in prose, emit literal newlines inside string values, and occasionally cite a passage number that was never supplied. All three are handled in code (`_extract_json`, `strict=False`, invalid-marker rejection) rather than by asking the model more firmly |
| Framework token overhead | **Real and measurable.** Answered questions run **1.2–1.7× slower** through Crew.AI than through direct calls: 60.0 s vs 35.5 s, and 66.5 s vs 54.5 s. Short-circuited questions are identical (~2.3 s) because they never reach an agent |
| Agent prompt wording | Roles and goals are filled from the **prompt registry**, so both paths change together and neither needs a redeploy |

### ⚠️ Implementation note: the framework was absent for a long time

The four roles were first built as plain async functions with **no Crew.AI at
all**, while `crewai` sat in `pyproject.toml` uninstalled and unimported and
docstrings across the read path pointed here as "the authority on control flow".
Nothing failed, because a dependency that is never imported breaks nothing.

That is now corrected — see [ADR 0001](../adr/0001-crewai-orchestration.md).
Both paths are kept and selected by `agents.use_crewai`, which is what makes the
overhead above a measurement rather than an opinion. The guards did **not** move
into the framework: parsing, the invented-scope guard, the `display_text`-only
rule and every degradation path stay in application code and are applied to the
crew's output exactly as to a direct call.

### Still open

| Item | Settled by |
|---|---|
| Whether the fast path materially changes answer quality | The ablation study |
| Whether the crew's extra latency is worth its structure at this model size | Evaluation, now that both paths are measurable |
