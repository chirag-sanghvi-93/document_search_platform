"""Turning `[n]` markers into references a reader can actually check.

The synthesizer numbers passages by *relevance*; a reader needs them numbered by
*appearance*, deduplicated to the thing they will physically look at, and free of
any marker that does not correspond to real evidence. That translation is this
module's whole job.

⚠️ Every displayed field is read from stored metadata — title, page, section,
`display_text`. **None of it is model output.** The model's only contribution is
the number, and even that is validated against the passages it was given. This is
the structural reason a citation cannot be fabricated: there is no path by which
a model-written filename or page could reach the reader, because the model is
never asked for one.

See doc/components/05-citation-handling.md — the authority.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.shared.types import Citation, Passage

logger = logging.getLogger(__name__)

#: A citation marker in the model's answer.
_MARKER = re.compile(r"\[(\d+)\]")

#: A bracketed numeral in SOURCE text — clause references, footnote markers,
#: enumerated sub-paragraphs. Policy and legal documents are full of them.
_BRACKETED_NUMERAL = re.compile(r"\[(\d+)\]")

#: Longest quote shown under a citation before truncation.
_QUOTE_MAX_CHARS = 400


def sanitize_for_prompt(text: str) -> str:
    """Neutralise bracketed numerals in passage text shown to the model.

    ⚠️ Without this, a citation can be spurious while parsing perfectly.

    Airline and insurance conditions are full of their own bracketed numerals —
    "[2]" as a clause reference, an enumerated sub-paragraph, a footnote marker.
    If the model reproduces that phrasing, the result is a marker that was never
    a marker: it parses cleanly, points at passage 2, and means nothing.

    Replacing `[2]` with `(2)` in the copy given to the model means every bracket
    it *sees* is one we introduced, so every bracket it *emits* is genuinely a
    citation. The text shown to the reader is untouched — this affects only the
    prompt.
    """
    return _BRACKETED_NUMERAL.sub(r"(\1)", text)


@dataclass(frozen=True)
class CitedAnswer:
    """An answer whose markers have been renumbered, with its source list."""

    answer: str
    citations: tuple[Citation, ...]

    #: Markers the model wrote that pointed at no passage it was given. Recorded
    #: rather than silently dropped — a non-zero rate here means the synthesizer
    #: is inventing references, which no amount of well-formed output reveals.
    invalid_markers: tuple[int, ...] = ()

    @property
    def has_citations(self) -> bool:
        return bool(self.citations)


def _merge_key(passage: Passage) -> tuple[str, int]:
    """What makes two passages the same citation.

    ⚠️ (file, page) — deliberately NOT (file, section, page) or chunk id.

    **The reader's unit of verification is a page.** They open the document and
    turn to page 14. Two chunks from different sub-sections of page 14 are, for
    that purpose, one source. Left unmerged they appear as `[1]` and `[3]`,
    which reads as two independent sources corroborating each other when it is
    one source cited twice — the opposite of the assurance a citation exists to
    give.
    """
    return (passage.source_file, passage.page)


def _quote(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _QUOTE_MAX_CHARS:
        return collapsed
    return collapsed[:_QUOTE_MAX_CHARS].rsplit(" ", 1)[0] + "…"


def build(answer: str, passages: list[Passage]) -> CitedAnswer:
    """Renumber the answer's markers by first appearance and build the sources.

    Four things happen here, and each corresponds to a way the naive version
    misleads a reader:

    1. **Renumbering by first appearance.** The model does not write in relevance
       order — it may open with passage 3 and never use passage 2. The reader
       then meets `[3]` before `[1]` with `[2]` missing, which reads as broken
       and undermines the very feature meant to build confidence
    2. **Merging on (file, page)**, so one page cited twice is one citation
    3. **Excluding uncited passages.** Listing everything retrieved "for
       transparency" is actively misleading: a source list asserts *this is where
       the answer came from*, and padding it implies support that does not exist
    4. **Dropping markers that point at nothing**, so a fabricated `[9]` never
       reaches the reader as a plausible-looking reference
    """
    if not answer:
        return CitedAnswer(answer=answer, citations=())

    by_number = {index: passage for index, passage in enumerate(passages, 1)}

    # First pass: walk the markers in the order the READER meets them, assigning
    # each distinct (file, page) its display number the first time it appears.
    assigned: dict[tuple[str, int], int] = {}
    ordered: list[Passage] = []
    invalid: list[int] = []

    for match in _MARKER.finditer(answer):
        original = int(match.group(1))
        passage = by_number.get(original)
        if passage is None:
            if original not in invalid:
                invalid.append(original)
            continue
        key = _merge_key(passage)
        if key not in assigned:
            assigned[key] = len(assigned) + 1
            ordered.append(passage)

    if invalid:
        logger.warning(
            "answer cited %s, which %s given to the synthesizer; dropping",
            ", ".join(f"[{n}]" for n in invalid),
            "was not a passage" if len(invalid) == 1 else "were not passages",
        )

    # Second pass: rewrite. Done with a single sub() over the original string so
    # that renumbering cannot collide with itself — mapping 1->2 and 2->1 by
    # successive replacement would turn both into the same number.
    def rewrite(match: re.Match[str]) -> str:
        original = int(match.group(1))
        passage = by_number.get(original)
        if passage is None:
            return ""
        return f"[{assigned[_merge_key(passage)]}]"

    rewritten = _MARKER.sub(rewrite, answer)
    # Dropping an invalid marker can leave " ." or a double space behind.
    rewritten = re.sub(r"\s+([.,;:!?])", r"\1", rewritten)
    rewritten = re.sub(r"[ \t]{2,}", " ", rewritten).strip()

    citations = tuple(
        Citation(
            number=assigned[_merge_key(passage)],
            source_file=passage.source_file,
            title=passage.title,
            page=passage.page,
            quote=_quote(passage.display_text),
            confidentiality=passage.confidentiality,
            section=passage.section,
        )
        for passage in ordered
    )

    return CitedAnswer(answer=rewritten, citations=citations, invalid_markers=tuple(invalid))


def near_misses(passages: list[Passage], *, limit: int = 3) -> tuple[Citation, ...]:
    """What was found when nothing answered the question.

    ⚠️ These are NOT sources, and whatever displays them must make that
    unmistakable — the risk is a reader skimming the label and treating them as
    support for an answer that was never given.

    They earn their place because a bare refusal is a dead end, whereas "these
    exist but do not answer it" is a next step: the reader learns adjacent
    material is there and can reformulate. Numbered from 0 so that nothing here
    can be confused with a real citation.
    """
    seen: set[tuple[str, int]] = set()
    out: list[Citation] = []
    for passage in passages:
        key = _merge_key(passage)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Citation(
                number=0,
                source_file=passage.source_file,
                title=passage.title,
                page=passage.page,
                quote=_quote(passage.display_text),
                confidentiality=passage.confidentiality,
                section=passage.section,
            )
        )
        if len(out) >= limit:
            break
    return tuple(out)


def render(citations: tuple[Citation, ...]) -> str:
    """The source list as text, for clients that cannot render structure."""
    lines: list[str] = []
    for citation in citations:
        location = f"{citation.title} · p.{citation.page}"
        if citation.section:
            location += f" · {citation.section}"
        lines.append(f'[{citation.number}] {location}\n    ▸ "{citation.quote}"')
    return "\n".join(lines)
