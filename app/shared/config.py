"""Application configuration.

Every tunable value in the system resolves from here. No module outside this file
reads the environment directly.

Settings are grouped into one class per concern rather than a flat namespace.
That is deliberate: `config.py` is the single file every epic needs to extend, and
a flat namespace would make it the file six parallel workers conflict on. Adding a
concern means adding a block, not a line in a shared list.

Environment variables use a double-underscore delimiter for nesting::

    DB__HOST=localhost
    OLLAMA__ANSWERING_MODEL=qwen3:8b
    RETRIEVAL__KEEP_FLOOR=0.35

Defaults come from the component documents under ``doc/components/``. Where a value
is provisional and settled by evaluation, it is marked below.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class DatabaseSettings(BaseModel):
    """PostgreSQL + pgvector.  See doc/components/02b-pgvector-postgresql.md"""

    host: str = "localhost"
    port: int = 5432
    user: str = "rag"
    password: str = "rag"
    name: str = "rag"

    # pgvector 0.8.0 introduced iterative index scanning, which filtered vector
    # search depends on. Without it, a filtered query silently returns fewer rows
    # than requested rather than erroring. Asserted at startup.
    min_pgvector_version: str = "0.8.0"

    # HNSW build parameters.
    hnsw_m: int = 16
    hnsw_ef_construction: int = 64

    # Query-time candidate list. Deliberately not the default of 40: it must exceed
    # the k being retrieved meaningfully, not marginally, or recall quietly suffers.
    hnsw_ef_search: int = 100

    text_search_config: str = "english"

    @property
    def url(self) -> str:
        return (
            f"postgresql+psycopg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"
        )

    @property
    def sync_url(self) -> str:
        """Driver-agnostic URL, for Alembic and psql."""
        return f"postgresql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class OllamaSettings(BaseModel):
    """Model hosting.  See doc/components/06-ollama.md — the authority on model selection."""

    host: str = "http://localhost:11434"

    embedding_model: str = "bge-m3"
    embedding_dimension: int = 1024

    # Document summarisation, during ingestion only. Off the critical path — it
    # runs once per document in a background Celery task, where a reasoning
    # model's extra tokens cost nobody any waiting.
    small_model: str = "qwen3:4b"

    # ⚠️ Contextualisation gets its own, NON-REASONING model, and this split is
    # the whole point of the setting existing.
    #
    # It is the highest-volume model call in the system — one per chunk, 589 for
    # our corpus — and the task is mechanical: write one situating sentence.
    # qwen3:4b is a reasoning model and generated 4,500-5,700 tokens to produce
    # that single sentence on a realistic 3.4k-char chunk, at ~194s each. That is
    # ~32 hours for one corpus.
    #
    # Thinking cannot be reliably disabled: `/no_think` had no effect at real
    # chunk size, and `think: false` on both /api/generate and /api/chat merely
    # moved the reasoning INTO the response field, which would corrupt preambles
    # with "We are given: - Document summary..." as their content.
    #
    # So the fix is a model that does not reason in the first place.
    contextualiser_model: str = "qwen2.5:3b"

    # ⚠️ Answering: a NON-reasoning instruct model, for the same reason as
    # `contextualiser_model` above.
    #
    # The design originally assigned qwen3:8b here on the grounds that reasoning
    # helps planning and synthesis. Defensible in principle, but it was never
    # costed: measured end to end, one question took **553 seconds** (9.2 min)
    # across 7 calls — roughly 79s each — against a design target of 30-90s for
    # the whole answer.
    #
    # Reasoning was kept at the time for the verifier, on the grounds that one
    # call per answer made it cheap. That turned out to be wrong too — see
    # `verifier_model` below. No reasoning model now runs on the read path at all.
    answering_model: str = "qwen2.5:7b"

    # ⚠️ Verification gets its own field for the same reason `contextualiser_model`
    # does: the axis that matters is per-call cost, not call volume.
    #
    # This was `small_model` (qwen3:4b) on the argument that one call per answer
    # made a reasoning model affordable. Measured, that one call was **177 of 233
    # seconds — 76% of the entire read path**. A smaller model was 18x SLOWER than
    # a larger one on the same prompt, purely from reasoning tokens.
    #
    # Speed was not the deciding test, though; verification is the last guard
    # against a fabricated claim reaching the user, so a draft containing an
    # invented "USD 150" was put to each candidate:
    #
    #     qwen3:4b     100.6s   retracted the fabrication
    #     qwen2.5:7b     8.8s   retracted it — byte-identical output
    #     qwen2.5:3b     4.6s   MISSED IT, passed the invented figure through
    #
    # So qwen2.5:3b is disqualified regardless of being fastest. qwen2.5:7b is
    # chosen: identical verification, 11x quicker, and — being the answering model
    # too — already resident, so verification costs no model switch either.
    #
    # small / cheap / adequate are three different axes. This design conflated
    # them three times: ingestion, then the read path, then here.
    verifier_model: str = "qwen2.5:7b"

    # Evaluation only, and from a different family than the answering model so the
    # judge is not grading its own output.
    judge_model: str = "gemma3:12b"

    # The HuggingFace repo whose tokenizer matches `answering_model`. Context
    # budgets are counted with it rather than estimated — see app/shared/tokens.py
    # for why an estimate is not good enough. Only the tokenizer files are
    # fetched, not the weights.
    tokenizer_repo: str = "Qwen/Qwen2.5-7B-Instruct"

    num_ctx: int = 8192

    # Zero everywhere. Determinism is worth more than variety in a cited system,
    # and it is what makes snapshot comparisons meaningful at all.
    temperature: float = 0.0

    request_timeout_s: int = 300

    # Health probes get their own, much shorter budget. Sharing the generation
    # timeout means an unreachable model host hangs /health/ready for five minutes —
    # long enough that an orchestrator times out the probe and reports nothing
    # useful about *why* the service is unready.
    health_timeout_s: int = 5


class IngestionSettings(BaseModel):
    """Write path.  See doc/components/01-docling.md and 03-contextual-agentic-rag.md"""

    raw_dir: Path = REPO_ROOT / "data" / "raw"
    processed_dir: Path = REPO_ROOT / "data" / "processed"
    preamble_dir: Path = REPO_ROOT / "data" / "preambles"

    # Sized to the embedding model, counted with the embedding model's own tokenizer.
    max_tokens_per_chunk: int = 768
    merge_peers: bool = True
    min_chunk_chars: int = 100

    default_collection: str = "default"


class RetrievalSettings(BaseModel):
    """Read path.  See doc/components/02-llamaindex.md and 02b §6"""

    # ⚠️ The collection the CHAT API answers from, and deliberately its own
    # setting rather than reusing `ingestion.default_collection`.
    #
    # Sharing them was a real bug. `ingestion.default_collection` is "default",
    # which is also the name of the seeded FIXTURE collection — 17 synthetic
    # chunks about a fictional airline. So the chat endpoint answered from the
    # fixtures instead of the 589-chunk corpus, fluently and with citations, and
    # the only tell was a page reference to a document nobody had uploaded.
    #
    # Ingesting and serving are different concerns and now have different knobs.
    collection: str = "corpus"

    vector_k: int = 20
    keyword_k: int = 20

    # Reciprocal Rank Fusion constant. Combines ranks, not scores, so the two
    # halves' incomparable magnitudes never have to be reconciled.
    rrf_k: int = 60

    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # Passages kept after re-ranking, per profile.
    keep_quality: int = 5
    keep_fast: int = 3

    # PROVISIONAL — the design's unfitted guess, not yet fit against this
    # corpus's own calibration split. See doc/components/09-ragas.md.
    keep_floor: float = 0.3
    insufficient_low: float = 0.3
    sufficient_high: float = 0.7


class AgentSettings(BaseModel):
    """Read-path control flow.  See doc/components/07-crewai.md — the authority."""

    # ⚠️ Whether the read path runs through Crew.AI agents or calls the models
    # directly. Both implement the SAME four roles and the same split of control
    # flow — the application owns the out-of-scope short-circuit and the fan-out,
    # the retrieval agent owns its retry loop.
    #
    # The flag exists because the framework's cost is real and measurable, not
    # because the choice is unsettled. Role, goal, backstory and task scaffolding
    # are prepended to every call and compete for the same 8192-token window that
    # has to hold instructions, evidence, memory and generation headroom
    # (doc/components/07-crewai.md §7). Keeping the direct path callable is what
    # makes that cost measurable rather than assumed, and it is what the
    # evaluation ablation compares.
    use_crewai: bool = True

    @property
    def crewai_available(self) -> bool:
        """Whether the crew path can actually run in THIS deployment.

        ⚠️ Checked at the point of use, never assumed from the flag alone.

        Turning `use_crewai` on by default in an image that did not ship the
        package made every chat request fail with ModuleNotFoundError raised from
        inside the planner — a config default depending on something the running
        image did not have. The read path has a fully working alternative sitting
        next to it, so failing outright was never the right response.

        Same shape as the missing cross-encoder: an optional dependency group a
        later epic started depending on, which nothing forced into the image. The
        difference is that this one degrades instead of breaking every request,
        and `/health/ready` reports it either way.
        """
        if not self.use_crewai:
            return False
        try:
            import crewai  # noqa: F401
        except ImportError:
            return False
        return True

    sub_question_cap: int = 4
    retry_cap: int = 2

    # Shared across all sub-questions, not per sub-question. Four sub-questions each
    # retrying twice would be fifteen model calls and a response no user waits for.
    search_budget: int = 6


class ConversationSettings(BaseModel):
    """Memory.  See doc/components/04-conversation-memory.md"""

    # ⚠️ The eviction TRIGGER is this budget, not the turn count below.
    #
    # Turn count is a proxy that fails badly: one turn is 50 tokens, the next is
    # 2,000 because a clause was pasted in. Four turns can therefore mean 200
    # tokens or 8,000 — and only one of those fits. The resource actually running
    # out is context, so it is measured directly.
    verbatim_budget_tokens: int = 1_200

    # A hard ceiling on top of the budget, not the trigger. It bounds how far back
    # a conversation of very short turns can reach, which keeps reference
    # resolution anchored to what was recently said.
    verbatim_turns: int = 4

    summary_max_tokens: int = 400

    # ⚠️ Memory truncates before evidence does. Never the other way round.
    #
    # Evidence is what the answer is supposed to be based on. If memory crowds it
    # out, the system answers from the conversation rather than from the
    # documents — silently, with nothing raised.
    evidence_priority: bool = True


class PhoenixSettings(BaseModel):
    """Prompts and tracing.  See doc/components/08-arize-phoenix.md — the authority."""

    endpoint: str = "http://localhost:6006"
    otlp_endpoint: str = "http://localhost:6006/v1/traces"

    serving_project: str = "rag-serving"
    ingestion_project: str = "rag-ingestion"

    prompt_dir: Path = REPO_ROOT / "prompts"
    prompt_tag: str = "production"
    prompt_cache_ttl_s: int = 60

    # Phoenix being unreachable must never make the backend unready. An
    # observability outage must not become an availability outage.
    required_for_readiness: bool = False


class CelerySettings(BaseModel):
    """Background tasks.  See doc/components/11-fastapi.md §5"""

    broker_url: str = "redis://localhost:6379/0"

    # No result backend, deliberately. Progress is read from ingestion_runs in
    # Postgres — the record that already exists for reproducibility — so there is
    # never a second, competing account of what a job is doing.
    ignore_result: bool = True

    ingestion_queue: str = "ingestion"
    default_queue: str = "default"

    # Load-bearing, not conservative. The pipeline batches by model; two concurrent
    # runs reintroduce the model thrash that batching exists to prevent.
    ingestion_concurrency: int = 1
    default_concurrency: int = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    log_level: str = "INFO"

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    agents: AgentSettings = Field(default_factory=AgentSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    phoenix: PhoenixSettings = Field(default_factory=PhoenixSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)


@lru_cache
def get_settings() -> Settings:
    """Resolved once per process. Cached so configuration cannot drift mid-run."""
    return Settings()
