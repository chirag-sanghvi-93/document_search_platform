# RAGAs

> Baseline item 9 of 10 — *"RAGAs — Evaluation of your Agentic RAG application"*.
>
> **This document is the authority on evaluation.** Other components state what must be measured;
> how it is measured, and what may honestly be claimed from it, is decided here.
>
> Satisfies requirement 3.4.

---

## 1. What it is

A library for evaluating retrieval-augmented systems. It computes metrics — faithfulness, answer
relevancy, context precision, context recall — and can synthesise a test set from a corpus.

**Its scope:** it computes metrics. It does not run the system, does not store results, and does not
decide what to test.

---

## 2. ⚠️ RAGAs covers about half of what must be measured

Evaluation has accumulated obligations from nearly every component:

| What must be measured | Source | RAGAs? |
|---|---|---|
| Faithfulness, relevancy, context precision/recall | Requirement 3.4 | ✓ |
| Ablation: contextual preambles on/off | Contextual | ✓ via recall |
| Ablation: retry loop on/off | Contextual | ✓ via context recall |
| Ablation: verifier on/off | Contextual | ✓ via faithfulness |
| Ablation: decomposition on/off | Contextual | ✓ partially |
| Ablation: fast path on/off | Crew.AI | ✓ |
| Ablation: re-ranker on/off | PGVector | ✓ via context precision |
| **Unanswerable questions — does it decline?** | Contextual | ✗ |
| **Multi-turn poisoning propagation** | Memory | ✗ |
| **Threshold calibration** | Contextual | ✗ |
| **Decision distributions — inert agents** | Crew.AI, Phoenix | ✗ |
| **Index recall — approximate vs exact** | PGVector | ✗ |
| **Citation coverage and validity** | Citations | ✗ |
| Chunk size tuning | Docling | ✓ indirectly |
| Embedding model comparison | Ollama | ✓ |

So RAGAs is **one component of an evaluation suite**, not the whole of it. Six of fifteen
obligations need a harness we write — worth being clear about now rather than discovering it when
evaluation is claimed complete. Six of them are ablations, which is what the cost model in §5
assumes.

---

## 3. The four metrics, and what each measures

Each maps onto a specific design decision, which is what turns evaluation into an argument rather
than four numbers:

| Metric | Question | Measures |
|---|---|---|
| **Faithfulness** | Is every claim supported by retrieved context? | The verifier |
| **Answer relevancy** | Does the answer address what was asked? | Synthesis |
| **Context precision** | Is retrieved context mostly signal? | The re-ranker |
| **Context recall** | Did retrieval find everything needed? | The retry loop and the contextual preambles |

Disable the verifier and faithfulness should drop. If it does not, the verifier is doing nothing —
one of the inert-agent failures the harness exists to detect.

---

## 4. Test set construction

### Three sets, four purposes

| Set | Built how | Purpose |
|---|---|---|
| **Generated** | RAGAs, from the corpus | Core metrics, ablations |
| **Unanswerable** | Hand-authored | Does it decline correctly |
| **Multi-turn** | Hand-constructed | Follow-ups, and poisoning |

Threshold calibration needs no fourth set — it wants questions with known-relevant chunks *and*
questions with none, which is sets 1 and 2 together. But see §7 on splitting them.

### Set 1 — Generated

**Size: ~70**, split into calibration and evaluation portions (§7).

**⚠️ Specify the question-type mix explicitly.** Left to default, generation skews toward simple
single-passage lookups — and a test set of simple lookups means decomposition never fires, so the
decomposition ablation measures nothing while reporting success.

| Type | Share | Exercises |
|---|---|---|
| Simple lookup | 50% | Baseline retrieval |
| Multi-hop | 30% | Decomposition, cross-document synthesis |
| Reasoning / conditional | 20% | The harder synthesis path |

**⚠️ Generate from `display_text`, not `embedding_text`.** Generating from the embedded version
produces questions about our synthetic preambles — questions whose answers are text we wrote rather
than text in the document. Evaluation would then measure how well the system retrieves its own
inventions.

**Curation is optional, deliberately.** Generated questions are sometimes malformed, ambiguous, or
carry incorrect ground truth, and pruning the worst improves results. But the pipeline must work
*without* curation, or the corpus-agnostic claim is false. Curation is a quality improvement for a
particular demonstration, not a step in the process.

### Set 2 — Unanswerable

Cannot be generated. RAGAs derives questions *from* chunks, so every generated question is answerable
by construction. This set must be written.

| Type | Example | Tests |
|---|---|---|
| **Adjacent but absent** | *"What is the surfboard fee?"* when only general sports equipment is covered | The score floor — retrieval **will** return something plausible |
| **Out of domain** | *"What is the weather in Abu Dhabi?"* | The out-of-scope short-circuit |
| **False premise** | *"What is the Platinum tier allowance?"* when no Platinum tier exists | Premise handling |

**Weighted toward the first.** Out-of-domain questions are easy — anything sensible declines them.
Adjacent-but-absent is where systems fail, because semantic search returns confidently
relevant-looking passages about the neighbouring topic and the model writes a plausible answer from
them.

**Size: ~30**, split per §7.

### Set 3 — Multi-turn

Hand-constructed conversations. Three scenarios, one positive and two negative:

**Follow-up resolution (positive).** *"What is the Economy allowance ABZ–LHR?"* → *"What about
Business?"* Does the rewrite produce a standalone question with the route preserved?

**Poisoning propagation (negative).** Seed a wrong assistant answer at turn 3, then ask a turn-5
follow-up that would use it. Does the error propagate, or does the verifier catch it because the
claim is not in *this turn's* evidence?

**Uncertainty laundering (negative).** Turn 3 declines — *"could not find a surfboard fee."* Force
summarisation. Turn 6 asks about surfboards again. Does the summary now assert *"surfboard fee: not
covered"* as established fact, or does the system search again?

The third is the subtlest failure in the entire design, and only this set finds it.

**Five to ten conversations.** Small, because each is expensive to run and they test specific
mechanisms rather than sampling a distribution.

### ⚠️ Version the test sets

A generated set is tied to the corpus it came from. Change the documents and it must be regenerated —
at which point results from before and after are **not comparable**, however similar the numbers
look.

Store each set with the corpus hash it was generated against, and refuse to compare runs across
different hashes. Otherwise a corpus change silently reads as a quality change.

---

## 5. The harness

### Shape

```
run(question_set, config)   →  outputs stored to disk
score(outputs)              →  metrics
compare(run_a, run_b)       →  deltas
```

The separation matters because **scoring is where iteration happens** — adding a metric, fixing a
judge prompt, correcting a threshold. If those require re-running the system, each fix costs an hour
rather than a minute.

### ⚠️ Ablations impose a requirement on the application

| Flag | Disables |
|---|---|
| `contextual_preambles` | The chunk preamble — **requires re-ingestion**, not a runtime flag |
| `verifier` | The verification stage |
| `retry_loop` | Retrieval retries — one search only |
| `decomposition` | Planner emits a single sub-question |
| `fast_path` | Always invoke the retrieval agent |
| `reranker` | Fusion output used directly |

**This must be designed in, not bolted on.** Each is a configuration branch in the application, and
retrofitting them once the code exists is considerably more painful.

The first differs in kind: the contextual ablation changes what is *in the index*, so it needs two
indexed corpora rather than a flag. It is the one ablation with a real setup cost.

### What gets stored per question

```
question, ground_truth
final_answer
passages           IDs + text
citations          markers and what they resolved to
decisions          intent, sub_questions, retries, retractions, declined
trace_id
config             which flags were set
prompt_versions
timings
```

Written as files under `eval_results/` — gitignored, being bulky and regenerable. **The summary
report is committed**, because evaluation is a deliverable and the report is what satisfies it.

### The six metrics RAGAs does not provide

**1 · Decline accuracy — measured in both directions**

| Metric | Set | Meaning |
|---|---|---|
| `false_answer_rate` | Unanswerable | Answered when it should not have. **The dangerous one** |
| `false_decline_rate` | Generated | Declined when it could have answered |

⚠️ **Both, or the metric is gameable.** A system that declines everything scores perfectly on the
unanswerable set; only the false-decline rate exposes it.

**2 · Path correctness**

Declining is not enough — each unanswerable type should decline *via its intended route*:

```
out-of-domain     →  retrieval_count == 0     (short-circuited)
adjacent          →  retries_used > 0         (tried before giving up)
false premise     →  declined, premise named
```

`retries_used > 0` rather than `== max`, because retries draw from a budget shared across
sub-questions — there is no single maximum a given sub-question is entitled to.

Read from the decision attributes. A system classifying everything as out-of-scope would decline
correctly while being badly broken.

**3 · Poisoning propagation**

Binary per conversation: did the seeded wrong fact appear in the later answer? Checks for the
specific injected error, so it is constructed per test case rather than generically.

**4 · Citation health — two metrics of different natures**

| | Nature |
|---|---|
| **Validity** — every marker resolves to a real passage | Deterministic, exact, cheap |
| **Density** — proportion of answers carrying citations | Approximate |

Precisely identifying "factual sentences" is harder than it appears, so density remains a proxy.
Validity is exact and worth checking every run — a rising invalid-marker rate means the model is
inventing references.

**5 · Index recall**

Not per-question. Run the retrieval queries with and without the vector index and compare the top-k
sets. Turns the `ef_search` guess into a measurement.

**6 · Decision distributions — as assertions, not metrics**

```
assert intent             takes more than one value
assert sub_question_count takes more than one value
assert retries_used       is non-zero at least once
assert claims_retracted   is non-zero at least once
```

**These fail the run.** A degenerate distribution means an agent is not deciding anything, and that
should be as loud as a crash — because it produces no other symptom. Expressing it as an assertion
rather than a number is what makes it impossible to overlook.

### ⚠️ Ablation cost, and containing it

```
6 runs × 50 questions × ~60s  ≈  5 hours of system time, plus scoring
```

Per full pass. Too slow to iterate on.

**Target each ablation at the questions that exercise it:**

| Ablation | Needs |
|---|---|
| Decomposition | Multi-hop questions only — ~15 |
| Verifier | Full set — it affects every answer |
| Retry loop | Full set plus unanswerable |
| Re-ranker | Full set |
| Fast path | Full set |
| Contextual | Full set, separate index |

Ablations measure a **delta**, not an absolute, so a subset is legitimate where the mechanism only
fires on certain question types. Roughly halves the cost.

---

## 6. ⚠️ The judge bias problem

RAGAs judges with a language model. If that is the same model which produced the answer, the system
grades its own homework — and models systematically prefer their own output.

| Option | Assessment |
|---|---|
| Same model as answering | Simplest, and measurably biased |
| **Different family as judge** | Reduces self-preference without better hardware |
| A larger model | Not available within the memory budget |

**Use a different model family for judging.** It does not eliminate the bias but removes its sharpest
form — and the limitation is stated in the results rather than presented as though it did not exist.

Note the mitigating factor from §7: bias inflates both arms of an ablation roughly equally, so it
largely cancels in deltas.

---

## 7. Interpreting results

### Establish the noise floor first

Temperature 0 is not fully deterministic, and the judge adds its own variance. So before any ablation
means anything:

> **Run the baseline twice, with identical configuration.**

The difference between those runs is the noise floor. Any ablation delta smaller than it is not a
signal, however suggestive.

One extra run, and the single most valuable thing in the evaluation — without it, every small delta
is a story waiting to be told.

### What the sample can detect

For a metric near 0.8, the standard error at n=50 is roughly 0.057 — a 95% interval spanning about
±11 points.

| Sample | Detectable difference |
|---|---|
| 50 generated questions | **~10 points or more** |
| 20 unanswerable | **~18 points or more** |

So 0.78 → 0.84 is nothing. It looks like an improvement and is indistinguishable from noise. Stating
this is not a weakness; it is what separates a measurement from a number.

### Deltas over absolutes

*"Faithfulness is 0.87"* means little — compared to what? A different corpus, judge, or test set
produces a different number, and there is no external scale.

*"Removing the verifier drops faithfulness by 0.15"* is a real claim: same corpus, same judge, same
test set, one thing changed.

Two consequences:

- **Never compare to published benchmarks.** Different corpus, different judge, different generation.
  The numbers are not commensurable.
- **Judge bias largely cancels in deltas**, since it inflates both arms roughly equally.

### ⚠️ Calibration leakage

Threshold calibration uses the generated and unanswerable sets. Evaluation then uses **the same
sets** — so thresholds are fitted to the test data, and decline accuracy is optimistic by
construction. Ordinary overfitting, arriving through a side door.

**Split them:**

```
calibration    20 generated  +  10 unanswerable    ← thresholds fitted here
evaluation     50 generated  +  20 unanswerable    ← never seen during calibration
```

Thresholds are chosen on the first and never revisited using the second.

### Diagnosing an unexpected result

When an ablation shows no effect, or the wrong direction, two very different explanations exist:

| Explanation | How to tell |
|---|---|
| **The mechanism is broken** | Decision distributions are degenerate — `retries_used` always 0, so the loop never fired |
| **The mechanism does not help here** | Distributions vary — it fired and made no difference |

**Check distributions before interpreting any flat result.** They distinguish *"the retry loop does
not help on this corpus"* from *"the retry loop never ran."*

### What the report must contain

| Section | Why |
|---|---|
| Corpus description **and hash** | Results are meaningless without knowing what was searched |
| Test set composition and sizes | |
| Configuration per run | Flags, models, prompt versions |
| **Noise floor** from the repeated baseline | Sets the bar for every other number |
| Delta table with the noise floor marked | The actual findings |
| **Limitations** | Judge bias, sample size, partly self-generated test set, corpus specificity |

### A negative result is still a result

If contextual retrieval does not improve recall on this corpus, **that is a finding** — report it. It
is more interesting than a confirmation, and it is honest.

The brief asks to *evaluate* the pipeline, not to prove it good. A report showing four mechanisms
helped and one did not reads as a genuine measurement. A report where everything worked exactly as
hoped reads as advocacy, and invites the question of what was not measured.

---

## 8. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Scope | RAGAs computes metrics; the harness does the rest | Six of fourteen obligations are ours |
| Generated set size | ~70, split calibration/evaluation | |
| **Question-type mix** | Specified explicitly — 50/30/20 | Default generation skews simple, making the decomposition ablation meaningless |
| **Generation source text** | `display_text` | Generating from `embedding_text` produces questions about our own preambles |
| Curation | Optional quality step, never a pipeline requirement | Otherwise the corpus-agnostic claim is false |
| Unanswerable set | **Hand-authored, ~30** | Cannot be generated — every generated question is answerable by construction |
| Unanswerable composition | Weighted toward adjacent-but-absent | Out-of-domain is easy; adjacent is where systems fail |
| Multi-turn set | 5–10 hand-built conversations | Follow-up resolution, poisoning, uncertainty laundering |
| Test set versioning | Stored with corpus hash; cross-hash comparison refused | A corpus change otherwise reads as a quality change |
| Harness shape | run / score / compare, separated | Scoring is where iteration happens |
| **Ablation flags** | Designed into the application from the start | Retrofitting configuration branches is far more painful |
| Contextual ablation | Two indexed corpora, not a flag | It changes the index, not the request path |
| Run outputs | `eval_results/`, gitignored | Bulky and regenerable |
| Summary report | **Committed** | It is the deliverable for requirement 3.4 |
| Decline accuracy | **Both false-answer and false-decline** | One direction alone is gameable |
| Path correctness | Verified from decision attributes | Declining for the wrong reason is still broken |
| Citation validity | Checked exactly, every run | A rising rate means invented references |
| **Decision distributions** | **Assertions that fail the run** | A degenerate distribution has no other symptom |
| Ablation scope | Targeted at questions exercising the mechanism | Deltas permit subsets; roughly halves the cost |
| Judge model | **Different family from the answering model** | Reduces self-preference bias. Affordable because evaluation is offline — the judge never coexists with the serving models |
| **Noise floor** | Baseline run twice, identical config | Deltas below it are not signals |
| Claimable difference | ~10 points at n=50; ~18 at n=20 | Stated in the report |
| Primary claims | **Deltas, not absolutes** | Absolutes have no external scale |
| External benchmarks | Never compared against | Not commensurable |
| **Calibration split** | Separate calibration and evaluation portions | Otherwise thresholds are fitted to the test data |
| Flat ablation results | Diagnosed against decision distributions first | Distinguishes "did not help" from "never ran" |
| Negative results | Reported, not suppressed | The brief asks for evaluation, not advocacy |

### Still open

| Item | Settled by |
|---|---|
| Which model family judges | Availability — the budget is not shared, since evaluation never runs while serving |
| Actual noise floor | The repeated baseline run |
| Whether 70/30 test set sizes are affordable given run cost | Measurement of a first full pass |
| Final threshold values | The calibration procedure, on the calibration split |
