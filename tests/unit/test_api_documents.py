"""The operator surface: upload, duplicate handling, and queue failure.

⚠️ The three behaviours asserted here are the ones where a plausible-looking
response is wrong: a 202 for work that was never queued, a re-ingestion of a
document already indexed, and a 200 for a file that is not a PDF. Each costs an
operator either an hour of model work or an hour of waiting for a run that will
never start.

Database and broker are both patched out — this is the HTTP translation layer,
not the engine.
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import documents
from app.main import create_app
from app.shared.config import get_settings
from app.shared.store import repository


@asynccontextmanager
async def _noop_lifespan(app: Any) -> AsyncIterator[None]:
    """Startup does tracing, prompt resolution and a cross-encoder warm-up. None
    of that is under test here, and the warm-up alone costs ~39 seconds."""
    yield


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    app = create_app()
    app.router.lifespan_context = _noop_lifespan
    return TestClient(app)


class _FakeSession:
    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _pdf() -> dict[str, Any]:
    return {"file": ("policy.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """No real database, no real broker, no writes into the repository's data dir."""
    monkeypatch.setattr(documents, "get_session", lambda: _FakeSession())
    monkeypatch.setattr(repository, "create_ingestion_run", lambda *a, **k: "run")

    settings = get_settings()
    monkeypatch.setattr(settings.ingestion, "raw_dir", tmp_path / "raw")


def test_a_non_pdf_is_refused(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "find_document_by_hash", lambda *a, **k: None)

    response = client.post(
        "/documents",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        data={"collection": "corpus"},
    )

    assert response.status_code == 400


def test_identical_content_returns_duplicate_and_queues_nothing(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ The check is synchronous and runs BEFORE enqueueing.

    Hashing a PDF costs well under a second, and it is the one piece of work that
    decides whether there is any work at all. Queueing first would mean paying
    for a full parse — minutes of model time — to discover the document was
    already indexed.
    """
    monkeypatch.setattr(
        repository,
        "find_document_by_hash",
        lambda *a, **k: {"id": "doc-1", "source_file": "policy.pdf", "title": "Policy"},
    )
    queued: list[Any] = []
    monkeypatch.setattr(documents, "_queue", lambda *a, **k: queued.append(a))

    response = client.post("/documents", files=_pdf(), data={"collection": "corpus"})

    assert response.status_code == 200
    assert response.json()["status"] == "duplicate"
    assert queued == [], "a duplicate must not enqueue any work"


def test_a_new_document_is_accepted_and_queued(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repository, "find_document_by_hash", lambda *a, **k: None)
    queued: list[Any] = []
    monkeypatch.setattr(documents, "_queue", lambda *a, **k: queued.append(a))

    response = client.post("/documents", files=_pdf(), data={"collection": "corpus"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["ingestion_run_id"]
    assert len(queued) == 1


def test_an_unreachable_broker_is_a_503_not_a_202(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ A 202 says "accepted, work will happen".

    Returning one for a message that was never queued leaves an operator
    watching a run that will never start, with nothing anywhere saying why. The
    request has to fail loudly instead.
    """
    monkeypatch.setattr(repository, "find_document_by_hash", lambda *a, **k: None)

    def _explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("redis unreachable")

    monkeypatch.setattr("app.tasks.ingestion.ingest_document.apply_async", _explode)

    response = client.post("/documents", files=_pdf(), data={"collection": "corpus"})

    assert response.status_code == 503


def test_metadata_is_optional_but_forwarded_when_given(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-supplied metadata overrides what ingestion would generate — the
    operator knows what the document is; the summariser only infers it."""
    monkeypatch.setattr(repository, "find_document_by_hash", lambda *a, **k: None)
    captured: list[Any] = []
    monkeypatch.setattr(documents, "_queue", lambda *a, **k: captured.append(a))

    client.post(
        "/documents",
        files=_pdf(),
        data={
            "collection": "corpus",
            "title": "Conditions of Carriage",
            "description": "baggage, fares and refunds",
        },
    )

    metadata = captured[0][3]
    assert metadata["title"] == "Conditions of Carriage"
    assert metadata["description"] == "baggage, fares and refunds"
    assert "effective_date" not in metadata, "unsupplied fields must not be sent as null"


def test_the_admin_page_is_served_and_makes_no_external_requests(client: Any) -> None:
    """⚠️ One self-contained file. This page curates a corpus that may be
    confidential and is served by the backend holding it — a single external
    request would tell a third party the deployment exists and, by referrer,
    something about what is on it."""
    response = client.get("/admin")

    assert response.status_code == 200
    body = response.text
    assert "<title>Corpus admin" in body
    for marker in ("http://", "https://", "//cdn", 'src="//'):
        assert marker not in body, f"admin.html reaches outside the deployment: {marker}"
