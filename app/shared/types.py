"""Shared contracts — the vocabulary every part of the system passes between layers.

⚠️ This is the highest-leverage file in the codebase. Everything downstream — the
write path, the read path, the API layer, the evaluation harness — imports from
here and nowhere else for these shapes. Getting a shape wrong here is expensive in
a way getting it wrong anywhere else is not: two independently-built pieces
inventing incompatible shapes for the same object is the actual risk in a system
built in parallel, not logical dependency ordering.

Deliberately importable with NOTHING else from this project. No ORM, no web
framework, no database driver. `eval/` must be able to import these types and
run without a server; the write and read paths must be able to run as a CLI. A
dependency on SQLAlchemy or FastAPI here would silently break both.

See doc/components/02b-pgvector-postgresql.md — the authority on the schema these
types mirror.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar, Literal

Confidentiality = Literal["public", "internal", "confidential"]
Role = Literal["user", "assistant"]
Provenance = Literal["cited", "hedged", "declined"]
IngestionStatus = Literal["running", "completed", "failed"]
#: `capability` is a question about the SYSTEM, not about the documents —
#: "what can I ask you?", "what documents do you have?". It is answered from the
#: corpus description without retrieval, because searching a baggage corpus for
#: the answer to "what can I ask you?" finds nothing and takes minutes to do it.
Intent = Literal["lookup", "comparison", "summary", "capability", "out_of_scope"]

# --------------------------------------------------------------------------- write path


@dataclass(frozen=True)
class Document:
    """A document-level record. Anything true of the WHOLE document lives here,
    never repeated on a chunk — see the governing rule in 02b-pgvector-postgresql.md:
    chunks carry chunk-level facts only.
    """

    id: str
    collection: str
    source_file: str
    doc_hash: str
    title: str
    ingestion_run_id: str

    # Operator-supplied, optional. Supplied overrides generated — see
    # doc/components/11-fastapi.md §4.
    description: str | None = None
    summary: str | None = None
    effective_date: date | None = None
    confidentiality: Confidentiality = "internal"
    extra: dict[str, object] = field(default_factory=dict)

    ingested_at: datetime | None = None


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit. `display_text` and `embedding_text` are separate,
    required fields, and must never be conflated — rendering `embedding_text`
    under a page citation would display words that do not appear on that page.

    `chunk_id` is deterministic — derived from `doc_hash` and `position` — so that
    the contextual preamble cache (keyed on chunk TEXT, not chunk_id) and the
    fixture corpus stay reproducible across runs.
    """

    id: str
    doc_id: str
    collection: str

    display_text: str
    embedding_text: str

    page: int
    position: int

    embedding: list[float] | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.display_text.strip():
            raise ValueError(f"chunk {self.id}: display_text must not be empty")
        if not self.embedding_text.strip():
            raise ValueError(f"chunk {self.id}: embedding_text must not be empty")


@dataclass(frozen=True)
class IngestionRun:
    """Reproducibility record. `config` is what turns 'what changed?' into a
    lookup rather than archaeology — see 02b-pgvector-postgresql.md §4.
    """

    id: str
    collection: str
    started_at: datetime
    status: IngestionStatus

    finished_at: datetime | None = None
    documents_seen: int = 0
    documents_parsed: int = 0
    documents_skipped: int = 0
    chunks_created: int = 0
    preambles_generated: int = 0
    config: dict[str, object] = field(default_factory=dict)
    error: str | None = None


# ---------------------------------------------------------------------------- read path


@dataclass(frozen=True)
class Passage:
    """A chunk returned from retrieval, carrying enough for both the synthesizer
    (display_text) and the citation renderer (source_file, page) without either
    needing a second lookup.
    """

    chunk_id: str
    doc_id: str
    source_file: str
    title: str
    page: int
    display_text: str
    score: float
    confidentiality: Confidentiality = "internal"

    #: Most specific heading above this chunk. Shown in a citation to say where
    #: to look on the page; never part of what identifies the citation.
    section: str = ""


@dataclass(frozen=True)
class Plan:
    """The planner's output. `sub_questions` drives the fan-out in the read path;
    `intent == out_of_scope` short-circuits before any retrieval happens at all.
    """

    intent: Intent
    standalone_question: str
    sub_questions: tuple[str, ...] = ()

    #: Intents answered without any retrieval at all.
    NO_RETRIEVAL: ClassVar[tuple[str, ...]] = ("out_of_scope", "capability")

    def __post_init__(self) -> None:
        if self.intent not in self.NO_RETRIEVAL and not self.sub_questions:
            raise ValueError("an answerable plan must have at least one sub-question")
        if len(self.sub_questions) > 4:
            raise ValueError(f"sub_question_cap is 4, got {len(self.sub_questions)}")


@dataclass(frozen=True)
class Citation:
    """One entry in the rendered source list. Merged on (source_file, page) —
    two cited chunks from the same page become one citation, not two.
    """

    number: int
    source_file: str
    title: str
    page: int
    quote: str
    confidentiality: Confidentiality = "internal"

    #: Where to look on the page. Displayed, but never part of the merge key —
    #: two sub-sections on one page are one citation to a reader who is turning
    #: to page 14 to check it.
    section: str = ""


@dataclass(frozen=True)
class ConversationSummary:
    """What the conversation established, as a schema rather than prose.

    ⚠️ The structure is the anti-poisoning guard, not tidiness.

    A prose summariser has no obligation to preserve a hedge, and compression
    strips qualifiers first — they are the least information-dense part of a
    sentence. So *"I couldn't find a surfboard fee in these documents"* becomes
    *"Surfboard fee: not covered"*, and an admission of ignorance has been
    laundered into a finding that later turns will reason from.

    `declined` exists so uncertainty has a named slot that a summariser is
    instructed to fill, instead of being the first thing compressed away.

    `parameters` exists for the opposite failure: *"The user asked about baggage
    allowances and was given the Economy limit"* reads well and is useless, because
    it dropped the route and fare class the next follow-up needs.

    See doc/components/04-conversation-memory.md §4 and §6.
    """

    #: Facts the user established: route, fare class, membership tier.
    parameters: tuple[str, ...] = ()
    #: Subjects already covered, so a follow-up can be recognised as one.
    topics: tuple[str, ...] = ()
    #: ⚠️ Questions asked and NOT answered. Never let these compress into topics.
    declined: tuple[str, ...] = ()
    #: Threads the user raised that have not been closed out.
    open_threads: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.parameters or self.topics or self.declined or self.open_threads)


@dataclass(frozen=True)
class Turn:
    """One persisted conversation turn. `trace_id` is the join key into the
    tracing system; `prompt_versions` is what answers 'which version of which
    prompt produced this' after the fact.
    """

    id: str
    conversation_id: str
    turn_index: int
    role: Role
    content: str

    rewritten_query: str | None = None
    citations: tuple[Citation, ...] = ()
    prompt_versions: dict[str, str] = field(default_factory=dict)
    provenance: Provenance | None = None
    trace_id: str | None = None
    latency_ms: int | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    created_at: datetime | None = None
