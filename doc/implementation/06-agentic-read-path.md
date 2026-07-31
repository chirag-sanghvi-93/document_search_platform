# Agentic Read Path — Build Record

> The sixth phase of the build: retrieved passages become a cited answer, and the
> system decides *how* to answer rather than following one fixed route.
>
> This document records what was built, how each behaviour was *verified* rather
> than assumed, and what went wrong along the way. The design documents say what
> should be built; this says what was.

---

## 1. What this phase covers

Satisfies baseline item 3 (**Contextual Agentic RAG**, read half) and delivers
requirement 2.1 — answers grounded in retrieved evidence, with the ability to
decline.

| Area | Delivers | Status |
|---|---|---|
| Planner | Classify, rewrite and decompose in one call | ✅ |
| Retrieval specialist | Judge sufficiency, reformulate, retry | ✅ |
| Synthesizer | Draft an answer carrying `[n]` markers | ✅ |
| Verifier | Ground each claim; retract what is unsupported | ✅ |
| Orchestration | Branching, fan-out, shared search budget | ✅ |
| Decision telemetry | Every decision on the **root** span | ✅ |
| Invented-scope guard | Reject narrowing the user did not ask for | ✅ |

### Explicit non-goals

- **No conversation memory** — `conversation_summary` and `recent_turns` are
  threaded through as parameters and currently receive `"(none)"`. The memory
  phase fills them; nothing here changes when it does
- **No citation rendering** — the answer carries `[n]` markers; turning those
  into page-anchored references is the citation phase
- **No HTTP endpoint** — `POST /chat` arrives with the API phase

---

## 2. The four agents and the control flow

The application owns branching and fan-out; an agent owns only what happens
inside a single call. That split is deliberate. The loop that matters (retry) and
the branch that matters (the out-of-scope short-circuit) are the *pipeline's*, so
they stay observable and testable without a model.

```
question
   │
   ▼
planner ────────── intent == out_of_scope ──────────▶ decline   (1 call, no search)
   │
   ▼  sub-questions
for each, within ONE SHARED budget:
   search ──▶ score high?  ──yes──▶ keep
        └──▶ retrieval specialist judges ──▶ reformulate ──▶ retry
   │
   ▼
deduplicate by chunk_id, then synthesize  ──▶  verify  ──▶  answer
```

### Call counts are a design commitment

Measured against the 589-chunk corpus, not asserted:

| Question | Intent | Calls | Searches | Retries | Provenance | Latency |
|---|---|---|---|---|---|---|
| "how do I bake sourdough bread?" | `out_of_scope` | **1** | 0 | 0 | declined | **2.2 s** |
| "what is the excess baggage charge?" | `lookup` | 3 | 1 | 0 | cited | 26.0 s |
| "am I covered if my baggage is lost?" | `lookup` | 3 | 1 | 0 | **hedged** | 33.5 s |
| "how do the baggage liability limits differ…" | `comparison` | 6 | 5 | **3** | cited | 93.7 s |

Three things in that table are the point of the phase:

- The out-of-scope question costs **one** model call and **zero** searches. It
  never touches the database
- The comparison question actually **retried three times** and spent five of its
  six shared searches — the retry loop is live, not decorative
- One lookup came back **hedged with a claim retracted**, so the verifier is
  demonstrably not a pass-through

### The shared search budget

The budget is spent from one pool across all sub-questions, not per
sub-question. Four sub-questions each retrying twice would be fifteen model
calls and a response nobody waits for. The comparison above used 5 of 6 and
stopped where it was told to.

### An empty result stays empty

Retrieval can return nothing (the score floor, recorded in
[05 · Retrieval](05-retrieval.md)). The read path preserves that: no passages
means the synthesizer is shown `(no passages retrieved)` explicitly rather than a
blank evidence block, because a blank block invites the model to answer from its
own knowledge.

---

## 3. Components: steps executed and how each was verified

### 3.1 Agents see `display_text`, never `embedding_text`

The structural guarantee behind honest citations. `embedding_text` carries a
model-written contextual preamble; if the synthesizer saw it, that generated
wording could be quoted under a page citation — attributing words to a page that
does not contain them.

Enforced in `_format_passages` and asserted directly:

```python
# tests/unit/test_agents_parsing.py
assert "The actual clause text." in rendered
assert "PREAMBLE ADDED BY A MODEL" not in rendered
```

### 3.2 Untrusted model output is parsed defensively

Every agent response is JSON produced by a model that was merely *asked* to
produce JSON. `_extract_json` takes the first `{…}` block rather than assuming
the whole response parses, and `_parse_plan` refuses an unrecognised intent
rather than defaulting to something plausible — a misread intent changes the
whole control flow, since `out_of_scope` skips retrieval entirely.

24 unit tests cover this layer with no model and no database.

### 3.3 Decision attributes on the **root** span

Several agentic behaviours fail into *inertness* while still producing
well-formed answers: a planner that always says `lookup`, a retry loop that never
fires, a verifier that never retracts. Each looks healthy per request; only the
distribution across many requests reveals it.

That requires the attributes to be on the root span — on a child span they are
invisible to the aggregate queries that would detect it. `intent`,
`sub_question_count`, `model_calls`, `searches_used`, `retries_used`,
`claims_retracted`, `claims_hedged`, `provenance`, `degraded` are all stamped
there before returning.

### 3.4 Provenance is derived from the answer, not the passage count

```python
has_citation_marker = bool(_CITATION_MARKER.search(verified.value.verified_answer))
if not passages or not has_citation_marker:
    provenance = "declined"
```

Verified by the run in §2: the sourdough question reports `declined`, and the
lookup that retracted a claim reports `hedged`.

---

## 4. Challenges and how they were resolved

### 4.1 ⚠️ A reasoning model on a per-request call — for the third time

**The single most expensive lesson in this project, and it recurred twice after
being learned.**

The first end-to-end run answered one question in **553 seconds**. The read path
was configured with `qwen3:8b` for planning, judging and synthesis, and
`qwen3:4b` for verification. The justification recorded in `config.py` was that
these are one call each per question — not one per chunk like contextualisation
— so a reasoning model's extra tokens were affordable.

Swapping the first three to `qwen2.5:7b` brought it to 354 s. Still far above the
30–90 s target, so the pipeline was instrumented per call rather than guessed at:

```
planner       qwen2.5:7b   3796 ch ->  289 ch     7.1 s
search        (cold cross-encoder load)          39   s
synthesizer   qwen2.5:7b   7531 ch ->  217 ch     9.9 s
verifier      qwen3:4b     7794 ch ->  291 ch   177.2 s   ← 76% of the request
```

**One call was 177 of 233 seconds.** A *smaller* model was 18× slower than a
larger one on a near-identical prompt, entirely because it emitted reasoning
tokens first.

Speed alone was not allowed to decide it. Verification is the last guard against
a fabricated claim reaching a user, so each candidate was given a draft
containing an invented figure — "The fee is exactly USD 150 per additional bag" —
over passages that never mention a number:

| Model | Time | Caught the fabricated figure? |
|---|---|---|
| `qwen3:4b` | 100.6 s | ✅ yes |
| `qwen2.5:7b` | **8.8 s** | ✅ yes — **byte-identical output** |
| `qwen2.5:3b` | 4.6 s | ❌ **no — passed "$150" through to the user** |

`qwen2.5:3b` is disqualified despite being fastest. `qwen2.5:7b` was chosen:
identical verification, 11× quicker, and — being the answering model too —
already resident, so verification costs no model switch either.

**Root cause.** *Small*, *cheap*, and *adequate* are three different axes, and
this design collapsed them into one three times: at contextualisation, then at
planning and synthesis, then at verification. Each time the argument was about
call *volume*; each time the thing that actually mattered was per-call cost.

A unit test now fails if any read-path model comes from a reasoning family, so
the fourth occurrence fails in CI instead of in a demonstration.

### 4.2 ⚠️ The planner narrowed the search to a document nobody asked about

Asked **"what is the excess baggage charge?"**, the planner produced:

> "What are the rules regarding excess baggage charges in **Delta Air Lines'
> domestic conditions of carriage**?"

Neither "Delta" nor "domestic" appears in the question. Because a sub-question
*is* the search query, this pulled the embedding toward one document: all five
retrieved passages were Delta, an Etihad clause that answers the question was
excluded, and the system **declined on a corpus that contained the answer**.

The planner prompt already forbade exactly this:

> *Do NOT add qualifiers the user did not state. If they did not say "domestic",
> "international", or name a specific document, neither should your
> sub-questions.*

It was violated on **every trial**, across two phrasings of the surrounding
corpus block. A variant that explicitly framed the corpus listing as
scope-checking only ("not a menu to choose from") changed the wording and kept
the narrowing.

**Root cause.** The corpus description is assembled one line per document, each
opening with the organisation that owns it. The planner reads that shape as a
menu. Confirmed by substituting a topic-only description with no organisation
names: the narrowing stopped immediately, and out-of-scope classification still
worked.

**Fix.** A code guard, not more prompting. `_reject_invented_scope` extracts
organisation and document identifiers from the corpus description and replaces
any sub-question introducing one the user never used — comparing against the
question *and* the conversation, so a name resolved from an earlier turn is
kept. The substitution is the user's own question, which is what a non-agentic
pipeline would have searched anyway. This matches how the rest of the module
treats model output: validate in code, do not trust the instruction.

The first version of this fix was **incomplete**, and the end-to-end run said so.
Retrieval was corrected — the top passage became the Etihad clause at 0.872 —
but the answer still read *"…for Delta Air Lines' domestic conditions of
carriage"*, now over passages that were no longer Delta-only. The guard had been
applied to `sub_questions` but not to `standalone_question`, which is what the
synthesizer is asked to answer. Retrieval and synthesis have to be narrowed, or
not narrowed, together.

**Known limitation.** The guard catches organisation and document identifiers,
not lowercase qualifiers such as "domestic". Those narrow less sharply because
they do not name a document, but they are not caught.

### 4.3 ⚠️ Prompt edits never reached the registry

`push_bundled()` compared **existence**, not content. Once a prompt existed under
the `production` tag, every later edit to the bundled file was silently skipped —
the registry kept serving the first version forever, so editing a prompt appeared
to do nothing while the file on disk showed the change.

This defeats the point of externalised prompts, and it cost a measurement: a run
intended to test a revised planner prompt was actually still running the original,
and the result would have been attributed to the wrong cause.

**Fix.** Compare content via the same round-trip recovery `_fetch()` already
relied on. Verified: push with one prompt edited reports 1 updated; an immediate
second push reports 0, so version history stays meaningful.

An earlier docstring claimed that bumping the `version:` frontmatter would force
a new version. **That was wrong** — the tag is fixed at `prompt_tag` and does not
vary with frontmatter. The claim has been corrected in place.

### 4.4 A refusal was recorded as a citation

Provenance was derived from whether passages existed. An answer that explicitly
refused — *"The passages provided do not specify…"* — while holding five passages
was recorded as `cited`. That is precisely the mislabelling that would corrupt
the decline-rate metric requirement 2.1 is measured by. Now a genuine citation
must carry at least one `[n]` marker.

### 4.5 Strict JSON parsing rejected good answers

The verifier returned a multi-sentence answer containing a literal newline inside
a JSON string value. Strict parsing rejected the whole object, the degradation
path fired, and a perfectly good answer was returned to the user marked
*"could not be verified"* — worse than the formatting it objected to.

**Fix.** `json.loads(..., strict=False)`, which permits literal newlines and tabs
inside string values while still rejecting genuinely malformed JSON such as a
trailing comma.

### 4.6 A stale test that had been failing unnoticed

`test_config.py` still asserted `answering_model == "qwen3:8b"` after the model
was changed. It had been red since the swap. The lesson from
[05 · Retrieval](05-retrieval.md) §4.1 repeats: the full suite has to be run, not
the file being worked on.

---

## 5. Final state

| Behaviour | Verified by |
|---|---|
| Out-of-scope costs 1 call, 0 searches | End-to-end run, §2 |
| Retry loop fires and respects the shared budget | 3 retries, 5 of 6 searches, §2 |
| Verifier retracts unsupported claims | Fabricated-figure test, §4.1; live `hedged` result, §2 |
| Agents never see `embedding_text` | Unit test, §3.1 |
| Invented document scope is removed | 8 unit tests + end-to-end, §4.2 |
| Malformed agent output degrades loudly | `AgentOutcome.degraded`, unit tests |

**54 unit tests pass; `mypy app tests` is clean; `ruff` is clean.**

Latency, end to end against the real corpus:

| | Before | After |
|---|---|---|
| Lookup | 553 s | **26–34 s** |
| Out of scope | — | **2.2 s** |
| Comparison (5 searches) | — | 93.7 s |

### Still open

- **26–34 s for a lookup is still slow**, and is *accepted for now* rather than
  solved. The decision was taken deliberately after the 21x reduction: the
  remaining cost is three sequential model calls on local CPU/Metal hardware, so
  further gains need a structural change (streaming the answer so time-to-first
  token replaces total latency, or running planner and retrieval concurrently),
  not another parameter. Revisit after the API phase, where streaming becomes
  available. **Nothing downstream is blocked by it**
- **Cold cross-encoder load costs ~39 s** on the first search in a process. It is
  amortised in a long-running server but is paid by the *first user*. Warming it
  at startup belongs with the API phase, where there is a lifespan hook to do it in
- **`keep_floor = 0.3` remains provisional**, to be fitted on the calibration
  split during evaluation
- **The comparison case at 93.7 s** sits at the top of the 30–90 s target. It is
  6 model calls and 5 searches, so it is not obviously wrong, but it is the case
  to watch
- **Lowercase qualifiers** ("domestic", "international") are not caught by the
  invented-scope guard

---

## 6. What this unblocks

Conversation memory has parameters waiting for it (`conversation_summary`,
`recent_turns`) and needs no change here. The citation phase consumes the `[n]`
markers the synthesizer already emits. The API phase wraps `answer_question` in
`POST /chat`. Evaluation reads the root-span attributes this phase records.

---

## 7. Command reference

```bash
# unit tests for this phase — no model, no database
uv run pytest tests/unit/test_agents_parsing.py -q

# the full gate
uv run pytest -m "not integration and not models and not live_phoenix" -q
uv run mypy app tests
uv run ruff check .
```
