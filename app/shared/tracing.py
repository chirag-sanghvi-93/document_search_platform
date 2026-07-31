"""Tracing.

One trace per request, spanning every stage. Auto-instrumentation covers the model
calls; the helpers here mark our own boundaries — the fan-out over sub-questions,
deduplication, citation assembly.

Two decisions carry the weight:

**Decision attributes go on the ROOT span**, not buried in children. That placement is
what makes them queryable in aggregate, and the aggregate is the point: several agentic
behaviours can fail into inertness while still producing well-formed output — a planner
that always returns ``lookup``, a retrieval agent that never retries, a verifier that
never retracts. Each looks healthy per request. Only a distribution that never varies
reveals it.

**Ingestion traces go to a separate project.** One ingestion run emits thousands of
spans; sharing a project would bury every request trace under them.

See doc/components/08-arize-phoenix.md — the authority on prompts and trace design.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

from app.shared.config import PhoenixSettings

logger = logging.getLogger(__name__)

Project = Literal["serving", "ingestion"]

_configured: set[str] = set()


def configure(settings: PhoenixSettings, project: Project = "serving") -> bool:
    """Wire the OTLP exporter. Returns True when tracing is live.

    Never raises. An unreachable collector must not stop the application — an
    observability outage must not become an availability outage.
    """
    name = settings.serving_project if project == "serving" else settings.ingestion_project
    if name in _configured:
        return True

    try:
        from phoenix.otel import register
    except ImportError:
        logger.warning("phoenix.otel not installed; tracing disabled")
        return False

    try:
        register(
            project_name=name,
            endpoint=settings.otlp_endpoint,
            auto_instrument=True,
            batch=True,
        )
    except Exception as exc:
        # Logged once, not per span. A collector that is down would otherwise
        # produce more log volume than the traces it failed to export.
        logger.warning("tracing unavailable (%s); continuing untraced", exc)
        return False

    _configured.add(name)
    logger.info("tracing to project '%s' at %s", name, settings.otlp_endpoint)
    return True


def flush(timeout_millis: int = 5000) -> bool:
    """Force the batch exporter to send pending spans now.

    ⚠️ ``BatchSpanProcessor`` exports on its own schedule, not immediately when a
    span ends — a fixed sleep after creating a span is a race, not a wait. This is
    the deterministic alternative, needed wherever spans must be visible before
    the next step runs: verification code, and short-lived CLI processes (the
    ingestion CLI, the eval harness) that could otherwise exit before their spans
    are ever sent.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return False

    provider = trace.get_tracer_provider()
    force_flush = getattr(provider, "force_flush", None)
    if force_flush is None:
        return False
    result: bool = force_flush(timeout_millis=timeout_millis)
    return result


def _tracer() -> Any | None:
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    return trace.get_tracer("document_search_platform")


@contextmanager
def span(name: str, **attributes: object) -> Iterator[Any | None]:
    """Mark one of our own boundaries.

    Yields ``None`` when tracing is unavailable, so call sites need no branching.
    """
    tracer = _tracer()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            current.set_attribute(key, _coerce(value))
        yield current


def set_root_attributes(**attributes: object) -> None:
    """Record decision attributes on the current span.

    Called at the top of a request so the attributes land on the ROOT span. Placing
    them on a child would make them invisible to the aggregate queries that detect
    inert agents.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        return

    current = trace.get_current_span()
    if current is None or not current.is_recording():
        return
    for key, value in attributes.items():
        current.set_attribute(key, _coerce(value))


def _coerce(value: object) -> str | int | float | bool:
    """OpenTelemetry accepts only primitives; anything else is stringified."""
    if isinstance(value, str | int | float | bool):
        return value
    return str(value)
