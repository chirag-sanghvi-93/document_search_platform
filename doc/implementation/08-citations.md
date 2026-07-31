# Citations — Build Record

> The eighth phase of the build: `[1]` stops being a character in a string and
> becomes a reference a reader can turn to and check.
>
> This document records what was built, how each behaviour was *verified* rather
> than assumed, and what went wrong along the way. The design documents say what
> should be built; this says what was.

---

## 1. What this phase covers

Satisfies baseline item 5 (**Citation handling using metadata**) and requirement
2.2 — a reader can verify any claim cheaply.

| Area | Delivers | Status |
|---|---|---|
| Collision guard | Source brackets neutralised before the model sees them | ✅ |
| Renumbering | By first appearance in the answer, not by relevance | ✅ |
| Merging | On (file, page) — the reader's unit of verification | ✅ |
| Exclusion | Uncited passages never enter the source list | ✅ |
| Invalid markers | Dropped from the answer, counted on the span | ✅ |
| Display | Title · page · section · quoted `display_text` | ✅ |
| Declined answers | "Closest matches", structurally distinct from sources | ✅ |
| Persistence | References stored per turn, not retrieved text | ✅ |

### Explicit non-goals

- **No deep linking.** `document.pdf#page=14` needs the documents to be servable,
  which depends on a decision about the client's documents that has not been made
- **No rendering surface.** `render()` produces text for clients that cannot
  display structure; the real presentation arrives with the frontend

---

## 2. The structural claim

> **Every displayed field is read from stored metadata. None of it is model
> output.**

Title, page, section and quote all come from the database. The model's only
contribution is the *number*, and even that is validated against the passages it
was given. There is no path by which a model-written filename or page reaches a
reader, because the model is never asked for one — which is what makes a
fabricated citation structurally impossible rather than merely unlikely.

---

## 3. Components: steps executed and how each was verified

### 3.1 ⚠️ The collision guard

**Policy and legal documents contain bracketed numerals of their own** — clause
references, enumerated sub-paragraphs, footnote markers. The corpus is full of
them: `8.2.1`, `15.2.2`, `[2]`.

If a passage contains `[2]` and the model echoes that phrasing back, the result
is a marker that was never a marker. It parses cleanly, points at passage 2, and
is entirely spurious.

So the passage text shown to the model has its brackets neutralised — `[2]`
becomes `(2)` — meaning every bracket the model *sees* is one we introduced, and
therefore every bracket it *emits* is genuinely a citation.

This affects only the prompt copy. The quote shown to the reader keeps the
document's own wording, brackets included, and a test asserts exactly that.

### 3.2 Renumbering by first appearance

Passages arrive ranked by relevance; the model does not write in that order. It
may open with passage 3, reach passage 1 in the second paragraph, and never use
passage 2 at all — so the reader meets `[3]` before `[1]` with `[2]` missing. It
reads as broken, and undermines the one feature meant to build confidence.

The rewrite is a single pass over the original string, because renumbering by
successive replacement cannot handle a swap: mapping 1→2 and then 2→1 turns both
into the same number. There is a test for precisely that.

### 3.3 Merging on (file, page)

```
5 passages  ->  3 citations        "am I covered if my baggage is lost?"
2 passages  ->  1 citation         "what is the excess baggage charge?"
```

Left unmerged, two chunks from page 14 appear as `[1]` and `[3]` — which reads as
two independent sources corroborating each other when it is one source cited
twice. **The reader's unit of verification is a page**: they open the document
and turn to page 14. Section is still displayed, because it says where to look
once there, but it is not what identifies the citation.

### 3.4 What a citation looks like

```
[1] etihad-general-conditions-of-carriage · p.35 · 8.2 EXCESS BAGGAGE
    ▸ "8.2.1 You will be required to pay a charge for carriage of Baggage in
       excess of the allocated Baggage allowance. These rates are available
       from us or our Authorized Agents upon request. …"
```

The quote is the decisive choice. Without it the reader must open the document to
verify, almost nobody will, and citations become decorative — present, unchecked,
and therefore not actually satisfying requirement 2.2. **A citation that is never
checked provides no assurance.**

### 3.5 Declined answers offer a next step

Verified live, and the contrast between the two kinds of decline is the point:

```
"what is the surfboard fee?"          [declined]
    The documents provided do not cover this question.
    Closest matches — these do NOT answer the question:
        delta-contract-of-carriage-international · p.13 · A. Checked and Carry-On Baggage
        delta-contract-of-carriage-domestic      · p.13 · A. Checked and Carry-On Baggage
        etihad-general-conditions-of-carriage    · p.66 · ARTICLE 17 - OTHER CONDITIONS

"how do I bake sourdough bread?"      [declined]
    That question is outside what these documents cover.
    (nothing offered)
```

The first was searched and found nothing above the floor, so adjacent material
exists and is worth surfacing. The second short-circuited as out-of-scope before
any search, so there is genuinely nothing to offer — and inventing something
would be worse than the bare refusal.

Near misses are numbered `0`, so nothing in the rendering path can confuse one
with a real citation.

---

## 4. Challenges and how they were resolved

### 4.1 ⚠️ A dataclass field that vanished in transit

Sections rendered empty on every citation, despite all 589 chunks carrying a
`heading_path` and the hydration query selecting it.

`rerank` rebuilt each `Passage` field by field:

```python
Passage(chunk_id=p.chunk_id, doc_id=p.doc_id, source_file=p.source_file, ...)
```

Adding `section` to the dataclass therefore dropped it *here*: retrieval carried
the heading through, re-ranking threw it away. **Nothing raised, no test failed,
no log line appeared** — the field was simply absent from the output, and an
absent section looks identical to a document that has no headings.

**Fix.** `replace(p, score=float(score))`. The construction only ever changes the
score, so it should only ever name the score, and any field added to `Passage` in
future now survives by default.

**The test is written against the dataclass's own field list** rather than
against `section` specifically, so the *next* field added is covered without
anyone remembering to extend it:

```python
for f in dataclasses.fields(original):
    if f.name == "score":
        continue
    assert getattr(result[0], f.name) == getattr(original, f.name)
```

That test then failed for an unrelated reason worth recording: it assumed the
cross-encoder would be unavailable in a unit test. It was not — it loaded, scored
the synthetic passage below `keep_floor`, and filtered it out entirely, so the
assertion failed on an empty list rather than on a dropped field. Replaced with a
stub encoder that scores above the floor, which exercises the rebuild path
without loading a model.

### 4.2 A designed feature that was dead code

`near_misses` existed and could never fire in the case it was built for.

`SearchResult` recorded `rejected_by_floor` as a **count**, discarding the
rejected passages themselves. So when the floor rejected *everything* — exactly
the "we searched and found nothing" case — `passages` was empty and there was
nothing to offer. The feature only worked when passages survived the floor but
went uncited, which is the rarer situation.

**Fix.** `SearchResult` now carries the rejected passages, and the scoring was
split so both sides of the floor come from **one** cross-encoder pass:

```python
scored, was_scored = rank(question, candidates, settings)
kept = [p for p in scored if p.score >= floor][:keep]
near = [p for p in scored if p.score < floor]
```

`rerank` is retained and now delegates to `rank`, so existing callers are
unaffected and the cross-encoder is never paid for twice.

**Worth keeping.** A count is a diagnostic; the design asked for a *next step*.
Recording the number satisfied the tracing requirement and quietly failed the
user-facing one, and unit tests passed throughout because they only ever asserted
the count.

---

## 5. Final state

| Behaviour | Verified by |
|---|---|
| Source brackets cannot become markers | Unit test |
| The reader still sees original wording | Unit test |
| Renumbered by first appearance; swaps survive | 3 unit tests |
| One page cited twice is one citation | Unit test + live (5→3, 2→1) |
| Uncited passages excluded | Unit test |
| Invalid markers dropped and counted | Unit test |
| Sections reach the citation | Live run + field-survival test |
| Declines offer closest matches, numbered 0 | Unit test + live |
| Out-of-scope offers nothing | Live |

**154 tests pass** — the whole suite, unit and integration, including live models
and a live Phoenix. `mypy app tests` is clean; `ruff` is clean.

### Still open

- **Deep linking is not built.** It needs the documents to be servable, which is
  a decision about the client's documents rather than a technical gap
- **Under-citation is not yet measured.** The design calls for the proportion of
  factual sentences carrying a marker as a health metric; `invalid_markers` is on
  the span, but the positive side is not. It belongs with evaluation
- **The two-markers-per-claim cap is prompt-only.** Nothing enforces it in code,
  so a model that appends four markers to a sentence produces four

---

## 6. What this unblocks

The API phase serialises `citations` and `near_misses` into the `/chat` response.
The frontend renders them collapsed, which is what makes verification cost one
interaction. Evaluation reads `invalid_markers` and the citation counts.

---

## 7. Command reference

```bash
# citation assembly — no model, no database
uv run pytest tests/unit/test_citations.py -q

# the full gate
uv run pytest -q
uv run mypy app tests
uv run ruff check .
```
