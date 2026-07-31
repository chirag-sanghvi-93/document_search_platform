# Citation Handling using Metadata

> Baseline item 5 of 10 — *"Citation handling using meta-data"*.
>
> A **mechanism**, not a tool. Nothing is installed for it — it operates entirely on metadata already
> attached to each chunk at ingestion.
>
> Satisfies requirement 2.2.

---

## 1. What it is

Every answer points back to where it came from — file, page, section.

The whole mechanism rests on one principle:

> **Citations are assembled, not generated.**

A model asked to cite its sources produces *"(Conditions of Carriage, p. 47)"* — correctly
formatted, plausibly specific, and frequently invented. It has no reliable access to page numbers; it
is pattern-matching what a citation looks like.

But the page number already exists. It was attached to the chunk at ingestion and travelled with it
through retrieval. The citation is therefore a **lookup**, not an act of recall.

### What this changes about the model's job

The model never writes a page number. It writes a **marker**:

```
Model sees      [1] "Cover does not apply where the property has been…"
                [2] "The allowance for Economy fares is 23 kg…"

Model writes    "Cover is excluded after 30 days unoccupied [1]."

System renders  [1] Conditions of Carriage · p.14 · 4.2 Exclusions
```

Its only decision is *which passage supports this claim* — a selection from what is in front of it.
Everything factual about the citation comes from metadata.

This reduces an unreliable task (recall a page number) to a reliable one (point at a numbered item).
That substitution is the entire design.

---

## 2. Use-cases covered

| # | Use-case | Purpose |
|---|---|---|
| 1 | Number retrieved passages before drafting | Gives the model something to point at |
| 2 | Parse markers out of the draft | Find what it pointed at |
| 3 | Validate markers against the passage set | Catch references to passages that do not exist |
| 4 | Renumber by first appearance | `[1]` should be the first one the reader meets |
| 5 | Deduplicate | Two chunks from the same place must not become two citations |
| 6 | Assemble the source list from metadata | File, page, section — never model output |
| 7 | Expose the supporting passage text | So a reader can check without opening the document |

---

## 3. Workflow

```
Ranked passages arrive
   │
1  Sanitize bracketed numerals in passage text   ← see §4
   │
2  Number them [1..N]                     [#1]
   │
3  Build the context block with markers
   │
4  Model drafts, using markers
   │
5  VERIFY — the draft is checked and may be rewritten
   │           ↑ everything below operates on the VERIFIED text
   │
6  Parse markers                          [#2]
   │
7  Validate against the passage set       [#3]  ← drops phantom references
   │
8  Group cited chunks by (file, page)     [#5]  ← deduplicate
   │
9  Order groups by first appearance       [#4]
   │
10 Assign new numbers to groups
   │
11 Rewrite the answer's markers                 ← chunk → its group's number
   │
12 Render the source list from metadata   [#6, #7]
```

Steps 6 onward operate purely on data already held. No model is involved, so nothing can go wrong
there that is not simply a bug.

**Numbering and parsing happen at different times, and the verifier sits between them.** Passages are
numbered *before* drafting, because the model needs something to point at. Markers are parsed,
validated and renumbered *after* verification, because the verifier rewrites the text that carries
them — and a retracted claim takes its marker with it.

**Order matters.** Deduplication must precede renumbering. If numbers are assigned first and merging
happens after, markers in the text point at a source that no longer has its own number and the
rewrite has to be undone.

---

## 4. Marker scheme

### The choice

`[1]`, `[2]`, `[3]` — numeric brackets.

| Option | Verdict |
|---|---|
| `[1]` | **Chosen.** Heavily represented in training data — academic text, encyclopedias. Format adherence is high without heavy prompting |
| `(policy.pdf, p.14)` | Rejected outright. Asking the model to write the filename and page *is* the fabrication being avoided |
| `<cite id="1"/>` | Reliable to parse, but verbose, and models drift out of it partway through an answer |
| `^1` | Collides with markdown and is easily lost in rendering |

`[1]` also degrades gracefully: if post-processing fails entirely, the reader still sees a
sensible-looking reference rather than raw markup.

### ⚠️ The collision problem

**Policy and legal documents contain bracketed numerals of their own** — clause references, footnote
markers, enumerated sub-paragraphs.

If a retrieved passage contains `[2]` and the model reproduces that phrasing, the result is a marker
that was never a marker. It parses cleanly, points at passage 2, and is entirely spurious.

**Guard: sanitize bracketed numerals in the passage text given to the model.** Replace `[2]` with
`(2)` when building the context block, so every bracket the model sees is one we introduced — and
therefore every bracket it emits is genuinely a citation.

This affects only the copy shown to the model. Text displayed to the reader remains original.

The alternative — a distinctive marker such as `[S1]` that cannot collide — trades collision risk for
format-adherence risk, since models follow the familiar pattern more reliably. Sanitizing removes the
problem deterministically, so the familiar format is kept.

### Multiple sources per claim

`[1][2]`, not `[1,2]`. Models produce the repeated-bracket form naturally, and it parses with one
expression rather than two.

**Capped at two per claim.** Uncapped, models append every available marker to every sentence — which
looks thorough and tells the reader nothing.

### Placement and granularity

**Per claim, not per paragraph.** A paragraph carrying one marker at the end leaves the reader unable
to tell which sentence it supports — and that reader is checking precisely because something looked
wrong.

**Immediately after the claim, before terminal punctuation:** `…for more than 30 days [1].`

The specific convention matters less than consistency, and consistency must be stated in the prompt
or both forms appear in one answer.

### What the prompt must instruct

1. Cite using `[n]`, where *n* is the passage number
2. Cite **every** factual claim
3. Use the most specific supporting passage — not every passage mentioning the topic
4. **Never write a number that was not provided**

The fourth reads as redundant and is not. Without it, a model with thin evidence still produces a
marker, because the pattern demands one.

### Over- and under-citation

| | Signal | Response |
|---|---|---|
| Over-citation | Markers on every sentence, several each | The cap, plus instruction 3 |
| Under-citation | Factual sentences carrying no marker | Measure it. The proportion of factual sentences with a citation is a useful health metric, and a sharp drop usually indicates retrieval was weak that turn |

---

## 5. Deduplication and ordering

### Ordering

Passages arrive ranked by relevance — `[1]` scored highest. The model does not write in that order.
It may open with passage 3, reach passage 1 in the second paragraph, and never use passage 2 at all.

The reader then meets `[3]` before `[1]`, with `[2]` missing. It reads as broken, undermining
confidence in the very feature meant to build it.

**Renumber by first appearance in the answer.** Retrieval order is an internal detail; presentation
order follows the reader.

### Uncited passages are excluded

A passage retrieved but never used does not enter the source list.

Worth stating, because the tempting alternative is listing everything retrieved "for transparency".
That is actively misleading: a source list asserts *this is where the answer came from*, and padding
it implies support that does not exist. The full retrieved set belongs in the trace.

### The merge key

Multiple chunks frequently originate from the same place. Left alone, the reader sees `[1]` and `[3]`
pointing at the same page — which reads as two independent sources corroborating one another. It is
one source, cited twice.

| Merge key | Effect |
|---|---|
| file + section | Fewest citations, but a long section spanning several pages leaves the reader hunting |
| **file + page** | **Chosen** — matches what the reader actually does |
| file + section + page | More precise, but splits adjacent text on one page into two citations for no benefit |

**The reader's unit of verification is a page.** They open the document and turn to page 14. Two
chunks on page 14 from different sub-sections are, for that purpose, the same citation.

Section is still *displayed*, since it indicates where to look on the page. It simply is not what
identifies the citation.

---

## 6. What a citation displays

```
[1] Conditions of Carriage · p.14 · 4.2 Exclusions
    ▸ "Cover does not apply where the property has been left
       unoccupied for more than 30 consecutive days."
```

| Field | Source | Notes |
|---|---|---|
| Document title | `documents.title`, falling back to filename | Reached by join from the chunk. `Conditions of Carriage` beats `coc_v3_final_2024.pdf` |
| Page | Metadata | The locator — what the reader acts on |
| Section | Heading path | The **most specific** heading only; the full path is verbose and rarely adds anything |
| Passage text | `display_text` | Expandable — see below |

All four are read from stored metadata. None is model output.

### The passage text is the decisive choice

| Option | Consequence |
|---|---|
| Not shown | The reader must open the document to verify. Almost nobody will, so citations become decorative — present, unchecked, and therefore not actually satisfying requirement 2.2 |
| **Shown, collapsed** | Verification costs one interaction. The option that makes citations do their job |
| Shown inline, expanded | Answers become enormous; five citations can dwarf the answer |

The argument is simple: **a citation that is never checked provides no assurance.** The entire value
lies in someone being able to confirm a claim cheaply, so anything raising the cost of checking
defeats the purpose.

⚠️ **The text shown must be `display_text`** — original document wording, never the search-optimised
version. Displaying embedded text beneath a page reference would show words that do not appear on
that page. This has been arranged since the chunk process; this is where it matters.

### Deep linking — conditional

With file and page, a link directly to the page is possible (`document.pdf#page=14`), turning
verification from *find the file, open it, scroll* into a single action.

Worth doing **if the documents are servable**. For confidential client documents that may not be
acceptable, in which case citations remain textual. Recorded as conditional rather than assumed,
since it depends on a decision about the documents that has not been made.

### What is not displayed

**Relevance scores.** Meaningful internally, misleading externally — a reader shown `0.72` reads it
as a confidence percentage, which it is not. Scores belong in the trace.

**Internal identifiers.** Chunk hash, document hash, position. All necessary; none informative to a
reader.

The rule: display what helps someone *find and check* the source. Everything else is diagnostic and
belongs where diagnostics go.

### Declined answers

When the system reports that the documents do not cover something, there are no sources — nothing
supported an answer, because there was none.

A bare refusal is unhelpful, though, and something useful is already to hand: **what was found, and
why it did not answer.**

```
The documents don't cover surfboard fees specifically.

Closest matches — these do not answer the question:
    Sports equipment allowances · p.22 · 3.4 Special baggage
    Excess baggage charges · p.19 · 3.1 Fees
```

Clearly labelled, this turns a dead end into a next step: the user learns adjacent material exists
and can reformulate.

The risk is a reader skimming past the label and treating them as sources, so the labelling must be
unambiguous and they must be **visually distinct** from real citations — not the same list under a
different heading.

---

## 7. Failure modes

| Failure | Detection | Response |
|---|---|---|
| **Phantom marker** — model emits `[7]` when given five passages | Validation, step 6 | Flag the claim as unsupported rather than silently dropping the marker. A phantom marker means the model believed it had evidence it did not have |
| **Marker points at the wrong passage** — valid format, real passage, does not support the claim | Not detectable by validation | Only groundedness checking catches this. Citation *validation* and citation *correctness* are different problems with different mechanisms |
| **Copied bracket from source text** | Prevented, not detected | Sanitization in step 1 |
| **Embedded text displayed under a page reference** | Not detectable automatically | Enforced structurally: only `display_text` is ever rendered |

The second row is the important distinction. Validation confirms a citation is *well-formed and
resolvable*. It says nothing about whether the cited passage actually supports the claim.

---

## 8. Decisions recorded here

| Decision | Choice | Note |
|---|---|---|
| Citation source | Assembled from metadata | Never authored by the model — the founding principle |
| Marker scheme | `[n]` numeric brackets | Highest format adherence; degrades gracefully |
| Bracket collision | Sanitize bracketed numerals in passage text given to the model | Source documents contain their own bracketed references |
| Multiple sources | `[1][2]`, capped at two per claim | Uncapped citation tells the reader nothing |
| Granularity | Per claim, not per paragraph | The reader checking needs to know which sentence |
| Placement | After the claim, before terminal punctuation | Consistency must be prompted or both forms appear |
| Presentation order | By first appearance in the answer | Retrieval rank is an internal detail |
| Uncited passages | Excluded from the source list | Padding implies support that does not exist |
| Merge key | **file + page** | The reader's unit of verification |
| Section | Displayed, not part of citation identity | |
| Sequence | **Verify** → validate → deduplicate → renumber → rewrite | Marker handling must follow verification, which rewrites the text carrying them |
| Passage text | Shown, collapsed via `<details>` in markdown | An unchecked citation provides no assurance. Falls back to a blockquote list if the renderer disallows raw HTML |
| Text variant | `display_text` only | Embedded text under a page reference shows words not on that page |
| Deep links | Conditional on documents being servable | Depends on an undecided question about the documents |
| Relevance scores | Not shown to readers | Retained in the trace |
| Phantom markers | Flag the claim, do not silently drop | The model believed it had evidence |
| Declined answers | Show closest matches, labelled and visually distinct | Turns a dead end into a next step |

### Still open

| Item | Decided under | Settled by |
|---|---|---|
| Whether documents are servable for deep linking | Not yet assigned | A decision about document handling, once the corpus arrives |
| Citation prompt wording | Arize Phoenix — design choice 5.8 | Evaluation |
| How the source list renders in the interface | OpenWebUI — design choice 5.9 | The interface's rendering capabilities |
| Target proportion of factual sentences carrying citations | This document | Measurement once answers exist |
