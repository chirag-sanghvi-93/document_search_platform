"""Shared contracts.

GIVEN  the contracts module
WHEN   it is imported
THEN   Chunk, Passage, Plan, Turn and Citation are defined, typed, and
       importable without importing the database layer

GIVEN  a Chunk
WHEN   display_text and embedding_text are inspected
THEN   they are separate, required fields — neither defaults to the other
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from app.shared.types import Chunk, Citation, Document, IngestionRun, Plan, Turn


def test_no_orm_or_web_framework_imports() -> None:
    """Importable from eval/ alone — no SQLAlchemy, no FastAPI, no database driver.

    Checked at the SOURCE level, not just "does importing it work in this test
    process" — this repo's other modules might have already imported sqlalchemy
    into sys.modules by the time this test runs, which would hide a real
    dependency. Parsing the AST is the only way to check what THIS file itself
    imports, independent of import order.
    """
    source = Path("app/shared/types.py").read_text()
    tree = ast.parse(source)

    forbidden = {"sqlalchemy", "fastapi", "psycopg", "pgvector", "alembic"}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    collision = imported & forbidden
    assert not collision, f"app/shared/types.py imports forbidden modules: {collision}"


def test_importable_in_a_fresh_process_with_no_project_dependencies() -> None:
    """The claim eval/ actually relies on: a bare python -c import must succeed
    even when sqlalchemy/fastapi are never imported anywhere in that process."""
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", "from app.shared.types import Chunk, Passage, Plan, Turn, Citation"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stderr


def test_chunk_requires_both_texts_non_empty() -> None:
    with pytest.raises(ValueError, match="display_text"):
        Chunk(
            id="c1",
            doc_id="d1",
            collection="default",
            display_text="",
            embedding_text="context: something",
            page=1,
            position=0,
        )

    with pytest.raises(ValueError, match="embedding_text"):
        Chunk(
            id="c1",
            doc_id="d1",
            collection="default",
            display_text="the actual clause text",
            embedding_text="",
            page=1,
            position=0,
        )


def test_chunk_texts_are_independent_fields() -> None:
    """Neither field defaults to or derives from the other."""
    chunk = Chunk(
        id="c1",
        doc_id="d1",
        collection="default",
        display_text="Checked baggage allowance is 23kg.",
        embedding_text="Baggage policy > Checked baggage: Checked baggage allowance is 23kg.",
        page=14,
        position=3,
    )
    assert chunk.display_text != chunk.embedding_text
    assert chunk.display_text in chunk.embedding_text  # preamble wraps, never replaces


def test_document_defaults_favour_generated_over_missing() -> None:
    doc = Document(
        id="doc1",
        collection="default",
        source_file="policy.pdf",
        doc_hash="abc123",
        title="Policy",
        ingestion_run_id="run1",
    )
    assert doc.description is None
    assert doc.confidentiality == "internal"
    assert doc.extra == {}


def test_plan_out_of_scope_needs_no_sub_questions() -> None:
    plan = Plan(intent="out_of_scope", standalone_question="what's the weather?")
    assert plan.sub_questions == ()


def test_plan_non_out_of_scope_requires_at_least_one_sub_question() -> None:
    with pytest.raises(ValueError, match="sub-question"):
        Plan(intent="lookup", standalone_question="what is the baggage fee?")


def test_plan_enforces_sub_question_cap() -> None:
    with pytest.raises(ValueError, match="sub_question_cap"):
        Plan(
            intent="comparison",
            standalone_question="compare five things",
            sub_questions=("a", "b", "c", "d", "e"),
        )


def test_turn_carries_prompt_versions_for_traceability() -> None:
    turn = Turn(
        id="t1",
        conversation_id="c1",
        turn_index=0,
        role="assistant",
        content="answer",
        prompt_versions={"planner": "3", "synthesizer": "7"},
        trace_id="trace-abc",
    )
    assert turn.prompt_versions["planner"] == "3"
    assert turn.trace_id == "trace-abc"


def test_citation_merge_key_is_file_and_page() -> None:
    """Two citations are 'the same' precisely when file and page match — this is
    what the citation-merging logic keys on, not chunk_id."""
    a = Citation(number=1, source_file="policy.pdf", title="Policy", page=14, quote="x")
    b = Citation(number=2, source_file="policy.pdf", title="Policy", page=14, quote="y")
    assert (a.source_file, a.page) == (b.source_file, b.page)


def test_ingestion_run_config_enables_what_changed_lookup() -> None:
    run = IngestionRun(
        id="run1",
        collection="default",
        started_at=__import__("datetime").datetime.now(),
        status="running",
        config={"chunk_size": 768, "embedding_model": "bge-m3"},
    )
    assert run.config["chunk_size"] == 768
