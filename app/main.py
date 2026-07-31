"""FastAPI application assembly.

Kept deliberately thin: this module wires routers and manages lifespan. It holds
no engine logic, because the engine must remain callable from the Celery worker
and the evaluation CLI, neither of which has a request context.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import anyio.to_thread
from fastapi import FastAPI

from app.api import chat, documents, health
from app.shared import tracing
from app.shared.config import Settings, get_settings
from app.shared.prompts import PromptRegistry

logger = logging.getLogger(__name__)


def _warm_reranker(settings: Settings) -> None:
    """Load the cross-encoder into the process cache. Blocking, hence a thread."""
    from app.engine.query.rerank import _load

    if _load(settings.retrieval) is not None:
        logger.info("cross-encoder warm: %s", settings.retrieval.reranker_model)


def _log_resolved_configuration(settings: Settings) -> None:
    """Log what configuration actually resolved to.

    Defaults are convenient and are also how a deployment ends up pointed at the
    wrong model or the wrong database without anyone noticing. Secrets are omitted.
    """
    logger.info("environment=%s log_level=%s", settings.environment, settings.log_level)
    logger.info("database=%s:%s/%s", settings.db.host, settings.db.port, settings.db.name)
    logger.info(
        "models embedding=%s(%d) small=%s contextualiser=%s answering=%s verifier=%s",
        settings.ollama.embedding_model,
        settings.ollama.embedding_dimension,
        settings.ollama.small_model,
        settings.ollama.contextualiser_model,
        settings.ollama.answering_model,
        settings.ollama.verifier_model,
    )
    logger.info("ollama=%s phoenix=%s", settings.ollama.host, settings.phoenix.endpoint)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    _log_resolved_configuration(settings)

    # Tracing first, so anything that follows is traced.
    tracing.configure(settings.phoenix, project="serving")

    # Push the bundled prompts into the registry, idempotently, then warm the cache.
    # Both steps tolerate an unreachable registry: the system serves bundled prompts
    # and records that it did so, rather than refusing to start.
    registry = PromptRegistry(settings.phoenix)
    registry.push_bundled()
    app.state.prompts = registry

    warmed = registry.resolve_all()
    logger.info(
        "prompts resolved: %d (%s)",
        len(warmed.versions),
        "degraded - bundled/cached" if warmed.degraded else "from registry",
    )

    # ⚠️ Warm the cross-encoder in the BACKGROUND, not before accepting traffic.
    #
    # Loading it costs ~39 seconds warm and, on a cold HuggingFace cache, ~5
    # minutes while 2.2 GB downloads. That cost is worth moving off the first
    # user's request — but it must not be moved onto startup, because FastAPI
    # accepts no connections until the lifespan's `yield`.
    #
    # Blocking here was tried and was wrong: the backend refused connections for
    # five minutes, and OpenWebUI — which fetches the model list once at boot —
    # got nothing, cached an empty list, and showed "No results found" in its
    # model picker long after the backend was healthy. A slow dependency became a
    # frontend that looked broken.
    #
    # So the app serves immediately and the model loads alongside it. A query
    # arriving during the window simply pays part of the load, exactly as it
    # would have without any warm-up.
    #
    # Failure is not fatal either: re-ranking degrades to fusion order and says
    # so — and `/health/ready` reports the reranker separately, so a deployment
    # that cannot load it at all is visible rather than merely slower.
    async def warm() -> None:
        try:
            await anyio.to_thread.run_sync(_warm_reranker, settings)
        except Exception as exc:
            logger.warning("cross-encoder warm-up failed (%s); searches degrade to fusion", exc)

    # Dependency verification lives in /health/ready rather than here, on purpose.
    # A backend that exits because Postgres is briefly down cannot report *why* it
    # is unavailable — it simply is not there. Starting and reporting unready is
    # more useful than refusing to start.
    #
    # The `yield` sits INSIDE the task group so the warm-up runs alongside a
    # serving app. Putting it after would make `async with` wait for the task to
    # finish, which is the blocking behaviour this exists to avoid.
    async with anyio.create_task_group() as warmup:
        warmup.start_soon(warm)
        yield
        # Shutdown: stop waiting on a download nobody will use.
        warmup.cancel_scope.cancel()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Document Search Platform",
        description="Agentic RAG over a curated PDF corpus",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(chat.router)
    return app


app = create_app()
