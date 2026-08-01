"""Health endpoints.

GIVEN  the backend is running and Postgres is stopped
WHEN   /health is called
THEN   200 — the process is alive

GIVEN  the same state
WHEN   /health/ready is called
THEN   503, naming Postgres as the unreachable dependency
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.shared.health import DependencyStatus, ReadinessReport


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_liveness_ignores_dependencies(client: TestClient) -> None:
    """Liveness must not consult anything external.

    A backend that cannot reach Postgres should not be killed and restarted —
    restarting it fixes nothing. That distinction is the reason there are two
    endpoints rather than one.
    """
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


def test_readiness_returns_503_and_names_the_failing_dependency(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def postgres_down(_settings: object) -> ReadinessReport:
        return ReadinessReport(
            dependencies=[
                DependencyStatus("postgres", "unavailable", "cannot connect: OperationalError"),
                DependencyStatus("ollama", "ok", "3 models present"),
                DependencyStatus("phoenix", "ok", required=False),
            ]
        )

    monkeypatch.setattr("app.api.health.readiness", postgres_down)

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False

    failing = [d for d in body["dependencies"] if d["status"] == "unavailable"]
    assert [d["name"] for d in failing] == ["postgres"]
    assert "cannot connect" in failing[0]["detail"]


def test_phoenix_being_down_does_not_make_the_backend_unready(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An observability outage must not become an availability outage."""

    async def phoenix_down(_settings: object) -> ReadinessReport:
        return ReadinessReport(
            dependencies=[
                DependencyStatus("postgres", "ok", "pgvector 0.8.0"),
                DependencyStatus("ollama", "ok", "3 models present"),
                DependencyStatus(
                    "phoenix", "degraded", "unreachable; serving bundled prompts", required=False
                ),
            ]
        )

    monkeypatch.setattr("app.api.health.readiness", phoenix_down)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["ready"] is True
