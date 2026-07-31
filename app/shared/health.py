"""Dependency probes behind the readiness endpoint.

Liveness and readiness answer different questions and are kept apart deliberately.
Collapsing them means a backend that cannot reach the model host either looks
healthy — and fails every request — or gets restarted repeatedly, which fixes
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from app.shared.config import Settings
from app.shared.models import OllamaClient, OllamaError

Status = Literal["ok", "degraded", "unavailable"]


@dataclass
class DependencyStatus:
    name: str
    status: Status
    detail: str = ""
    required: bool = True


@dataclass
class ReadinessReport:
    dependencies: list[DependencyStatus] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Ready when every *required* dependency is usable.

        Phoenix is deliberately not required: an observability outage must not
        become an availability outage.
        """
        return all(d.status == "ok" for d in self.dependencies if d.required)


def _parse_version(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_postgres(settings: Settings) -> DependencyStatus:
    """Verify the database is reachable, has pgvector, and that pgvector is new enough.

    The version check is not ceremony. Filtered vector search depends on iterative
    index scanning, introduced in pgvector 0.8.0. On an older build the query
    returns fewer rows than requested — silently, with no error — which reads as a
    retrieval quality problem rather than a configuration one.
    """
    try:
        engine = create_engine(settings.db.url, pool_pre_ping=True)
        with engine.connect() as conn:
            installed = conn.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
    except SQLAlchemyError as exc:
        return DependencyStatus(
            "postgres", "unavailable", f"cannot connect: {exc.__class__.__name__}"
        )

    if installed is None:
        return DependencyStatus(
            "postgres", "unavailable", "the 'vector' extension is not installed"
        )

    minimum = settings.db.min_pgvector_version
    if _parse_version(str(installed)) < _parse_version(minimum):
        return DependencyStatus(
            "postgres",
            "unavailable",
            f"pgvector {installed} is older than the required {minimum}; "
            "filtered vector search would silently return short result sets",
        )

    return DependencyStatus("postgres", "ok", f"pgvector {installed}")


async def check_ollama(settings: Settings) -> DependencyStatus:
    """Verify the model host is reachable and the three serving models are present."""
    client = OllamaClient(settings.ollama, for_health_check=True)
    try:
        availability = await client.check_models()
    except OllamaError as exc:
        return DependencyStatus(
            "ollama",
            "unavailable",
            f"{exc} — if running natively, start it with OLLAMA_HOST=0.0.0.0 "
            "so containers can reach it",
        )
    finally:
        await client.aclose()

    if not availability.ok:
        return DependencyStatus(
            "ollama",
            "unavailable",
            f"missing models: {', '.join(availability.missing)} — run `make models`",
        )
    return DependencyStatus("ollama", "ok", f"{len(availability.available)} models present")


async def check_phoenix(settings: Settings) -> DependencyStatus:
    """Reachability only, and never required for readiness."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(settings.phoenix.endpoint)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return DependencyStatus(
            "phoenix",
            "degraded",
            f"unreachable ({exc.__class__.__name__}); serving bundled prompts",
            required=settings.phoenix.required_for_readiness,
        )
    return DependencyStatus("phoenix", "ok", required=settings.phoenix.required_for_readiness)


def check_reranker(settings: Settings) -> DependencyStatus:
    """⚠️ Verify the cross-encoder can actually be loaded.

    This is a readiness check rather than a warning because of what its absence
    does. Without `sentence-transformers` there is no cross-encoder, therefore no
    score floor, therefore retrieval can never return an empty result — and an
    empty result is the entire mechanism behind declining rather than
    confabulating. The system keeps answering; it simply loses the ability to say
    "the documents do not cover this".

    Nothing about the output reveals it. Answers stay fluent and carry citations,
    /health/ready returned 200, and the only signal was a single WARNING at
    startup. A container ran in that state for 39 hours before anyone noticed.
    Degrading quietly is defensible; degrading quietly *while reporting ready* is
    not.
    """
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        return DependencyStatus(
            name="reranker",
            status="unavailable",
            detail=(
                "sentence-transformers not installed: no cross-encoder, no score "
                "floor, and therefore no ability to decline"
            ),
        )
    return DependencyStatus(name="reranker", status="ok", detail=settings.retrieval.reranker_model)


def check_agent_framework(settings: Settings) -> DependencyStatus:
    """⚠️ Verify Crew.AI is importable when the read path is configured to use it.

    `use_crewai` defaults to on, and without the package EVERY chat request
    returns a 500 from deep inside the planner. That is loud, but it is loud in
    the wrong place: the first user finds it, not the deployment. This is the
    same omission that left the image without a cross-encoder — an optional
    dependency group that a later epic started depending on and nobody added to
    the image.
    """
    if not settings.agents.use_crewai:
        return DependencyStatus(
            name="agent-framework",
            status="ok",
            detail="direct path (use_crewai=false)",
            required=False,
        )
    try:
        import crewai
    except ImportError:
        # Degraded, not unavailable: the read path falls back to direct model
        # calls and still answers. Reporting this as a hard failure would take a
        # working service out of rotation over a framework it can run without.
        return DependencyStatus(
            name="agent-framework",
            status="degraded",
            detail="crewai requested but not installed; running the direct path. "
            "Build with --extra agents to enable it.",
            required=False,
        )
    return DependencyStatus(
        name="agent-framework", status="ok", detail=f"crewai {crewai.__version__}"
    )


async def readiness(settings: Settings) -> ReadinessReport:
    return ReadinessReport(
        dependencies=[
            check_postgres(settings),
            await check_ollama(settings),
            check_reranker(settings),
            check_agent_framework(settings),
            await check_phoenix(settings),
        ]
    )
