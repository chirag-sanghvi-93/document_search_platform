"""The fixture corpus.

GIVEN  a clean database
WHEN   `make seed` is run
THEN   several dozen chunks across at least three documents exist, with real
       embeddings, spanning at least two collections

GIVEN  the fixtures are loaded twice
WHEN   the chunk count is compared
THEN   it is identical — seeding is idempotent and deterministic
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.shared.store.engine import get_session
from app.shared.store.seed import seed
from tests.fixtures.fixture_chunks import CHUNKS, DOCUMENTS

pytestmark = pytest.mark.integration


_FIXTURE_IDS = [c.chunk_id for c in CHUNKS]


def _fixture_chunk_count() -> int:
    """Scoped to the fixture's OWN ids, deliberately — not a raw `count(*)`.

    A shared dev database is not this test's alone: other suites (see
    test_repository.py) write and clean up their own rows in the same table.
    An unscoped count is fooled by whatever else happens to exist at the
    moment this runs; filtering to ids this fixture set itself defines cannot be.
    """
    with get_session() as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM chunks WHERE id = ANY(:ids)"), {"ids": _FIXTURE_IDS}
            ).scalar_one()
        )


def _collections_present() -> set[str]:
    with get_session() as session:
        rows = session.execute(
            text("SELECT DISTINCT collection FROM chunks WHERE id = ANY(:ids)"),
            {"ids": _FIXTURE_IDS},
        ).all()
        return {r.collection for r in rows}


def test_seed_loads_several_dozen_chunks_across_documents_and_collections() -> None:
    seed()

    assert _fixture_chunk_count() == len(CHUNKS)
    assert len({c.doc_hash for c in DOCUMENTS}) >= 3
    assert len({d.collection for d in DOCUMENTS}) >= 2
    assert _collections_present() >= {"default", "cargo"}


def test_seeding_twice_is_idempotent() -> None:
    seed()
    first_count = _fixture_chunk_count()

    seed()
    second_count = _fixture_chunk_count()

    assert first_count == second_count == len(CHUNKS)


def test_seeded_chunks_carry_real_1024_dimension_embeddings() -> None:
    seed()
    with get_session() as session:
        row = session.execute(
            text(
                "SELECT vector_dims(embedding) AS dims FROM chunks WHERE id = 'fx-baggage-01__000'"
            )
        ).one()
    assert row.dims == 1024


def test_near_duplicate_pair_is_closer_than_unrelated_chunks() -> None:
    """The fixture design depends on this: two chunks about the same topic in
    different documents must be semantically close, or RRF/dedup testing later
    has nothing real to exercise."""
    seed()
    with get_session() as session:
        rows = session.execute(
            text(
                """
                SELECT id, embedding <=> (
                    SELECT embedding FROM chunks WHERE id = 'fx-baggage-01__001'
                ) AS distance
                FROM chunks
                ORDER BY distance
                LIMIT 3
                """
            )
        ).all()

    ids_in_order = [r.id for r in rows]
    assert ids_in_order[0] == "fx-baggage-01__001"  # itself, distance 0
    assert ids_in_order[1] == "fx-coc-01__002"  # the intended near-duplicate


def test_rare_token_is_findable_by_keyword_search() -> None:
    seed()
    with get_session() as session:
        rows = session.execute(
            text("SELECT id FROM chunks, plainto_tsquery('english', 'EY360A') q WHERE tsv @@ q")
        ).all()

    ids = {r.id for r in rows}
    assert ids == {"fx-dg-01__000", "fx-dg-01__001"}
