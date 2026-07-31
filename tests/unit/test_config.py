"""E1-S2 · Configuration.

GIVEN  a setting absent from the environment
WHEN   the application starts
THEN   the documented default is used

GIVEN  a nested setting supplied via the environment
WHEN   settings resolve
THEN   the supplied value wins
"""

from __future__ import annotations

import pytest

from app.shared.config import Settings


def test_defaults_come_from_the_component_documents() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    # doc/components/06-ollama.md — the authority on model selection
    assert settings.ollama.embedding_model == "bge-m3"
    assert settings.ollama.embedding_dimension == 1024
    assert settings.ollama.answering_model == "qwen2.5:7b"
    assert settings.ollama.verifier_model == "qwen2.5:7b"
    assert settings.ollama.small_model == "qwen3:4b"
    assert settings.ollama.temperature == 0.0
    assert settings.ollama.num_ctx == 8192

    # The contextualiser is deliberately a DIFFERENT, non-reasoning model from
    # small_model. It is the highest-volume call in the system (one per chunk),
    # and a reasoning model spent ~5,000 tokens and ~194s producing one sentence
    # — ~32 hours for a 589-chunk corpus. See app/shared/config.py.
    assert settings.ollama.contextualiser_model == "qwen2.5:3b"
    assert settings.ollama.contextualiser_model != settings.ollama.small_model


#: Model families that emit chain-of-thought before their answer. Kept as a
#: prefix list rather than exact tags so a future `qwen3:14b` is caught too.
_REASONING_FAMILIES = ("qwen3:",)


def test_no_reasoning_model_sits_on_the_read_path() -> None:
    """⚠️ This project assigned a reasoning model to a per-request call THREE
    times, and measured it as a mistake three times.

    Contextualisation: ~194s per chunk, ~32 hours for one corpus.
    Planning and synthesis: 553s for a single answer.
    Verification: 177s of a 233s answer — 76% of the read path, in one call.

    Each time the justification was that the call volume was low, and each time
    volume was the wrong axis: what matters is the per-call cost of a model that
    emits thousands of reasoning tokens before it answers. Reasoning is fine in
    `small_model`, which runs only during background ingestion.

    This test exists so the fourth time fails in CI instead of in a demo.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    for field in ("answering_model", "verifier_model", "contextualiser_model"):
        model: str = getattr(settings.ollama, field)
        assert not model.startswith(_REASONING_FAMILIES), (
            f"{field}={model} is a reasoning model on a latency-critical path; "
            "see the note on verifier_model in app/shared/config.py"
        )

    # doc/components/02b-pgvector-postgresql.md
    assert settings.db.hnsw_ef_search == 100, "the default of 40 is too close to a k of 20"
    assert settings.db.hnsw_m == 16
    assert settings.db.hnsw_ef_construction == 64

    # doc/components/02-llamaindex.md
    assert settings.retrieval.rrf_k == 60
    assert settings.retrieval.vector_k == 20
    assert settings.retrieval.keyword_k == 20

    # doc/components/07-crewai.md — the authority on read-path control flow
    assert settings.agents.sub_question_cap == 4
    assert settings.agents.search_budget == 6


def test_nested_environment_variables_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB__HOST", "somewhere-else")
    monkeypatch.setenv("RETRIEVAL__KEEP_FLOOR", "0.55")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.db.host == "somewhere-else"
    assert settings.retrieval.keep_floor == 0.55
    # Untouched siblings keep their defaults rather than resetting.
    assert settings.db.port == 5432


def test_database_url_is_assembled_from_parts() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.db.url.startswith("postgresql+psycopg://")
    assert f":{settings.db.port}/" in settings.db.url


def test_phoenix_is_not_required_for_readiness() -> None:
    """An observability outage must not become an availability outage."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.phoenix.required_for_readiness is False


def test_ingestion_concurrency_is_one() -> None:
    """Load-bearing, not conservative.

    The pipeline batches by model. Two concurrent runs reintroduce the model
    thrash that batching exists to prevent, from outside the pipeline where
    nothing inside it can attribute the cause.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.celery.ingestion_concurrency == 1


def test_no_result_backend_is_configured() -> None:
    """Structurally prevents a second, competing record of job state."""
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.celery.ignore_result is True


def test_the_serving_collection_is_not_the_fixture_collection() -> None:
    """⚠️ A real bug, and a silent one.

    The chat API originally read `ingestion.default_collection`, which is
    "default" — also the name of the seeded FIXTURE collection of 17 synthetic
    chunks about a fictional airline. Every answer came from the fixtures,
    fluently and with citations; the only tell was a citation to a document
    nobody had ever uploaded.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.retrieval.collection != settings.ingestion.default_collection
    assert settings.retrieval.collection == "corpus"
