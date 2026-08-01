"""Service acceptance criteria.

These need real services. Run with `make test`; skipped by `make test-unit`.

    GIVEN  the stack is up
    WHEN   `CREATE EXTENSION vector;` is executed
    THEN   it succeeds, and extversion supports iterative index scanning

    GIVEN  bge-m3 is available
    WHEN   a string is embedded
    THEN   a vector of exactly 1024 dimensions is returned
"""

from __future__ import annotations

import pytest

from app.shared.config import get_settings
from app.shared.health import _parse_version, check_postgres
from app.shared.models import OllamaClient


@pytest.mark.integration
def test_pgvector_is_installed_and_new_enough() -> None:
    """An older pgvector makes filtered vector search return short result sets.

    Not wrong — short, and silently. Nothing surfaces it except this assertion.
    """
    settings = get_settings()
    status = check_postgres(settings)

    assert status.status == "ok", status.detail
    assert "pgvector" in status.detail

    installed = status.detail.removeprefix("pgvector ").strip()
    assert _parse_version(installed) >= _parse_version(settings.db.min_pgvector_version)


@pytest.mark.models
async def test_all_three_serving_models_are_present() -> None:
    settings = get_settings()
    client = OllamaClient(settings.ollama)
    try:
        availability = await client.check_models()
    finally:
        await client.aclose()

    assert availability.ok, f"missing: {availability.missing} — run `make models`"


@pytest.mark.models
async def test_embedding_dimension_matches_configuration() -> None:
    """The assertion that must happen before the chunks table is created.

    ``vector(N)`` is fixed at table creation. A mismatch discovered after
    ingestion means dropping the column and re-embedding the whole corpus.
    """
    settings = get_settings()
    client = OllamaClient(settings.ollama)
    try:
        dimension = await client.check_embedding_dimension()
    finally:
        await client.aclose()

    assert dimension == settings.ollama.embedding_dimension == 1024
