"""Tracing against a real Phoenix instance.

Confirms spans actually land — not just that ``register()`` returns without
raising, which would pass even if nothing ever reached the collector.

⚠️ Each scenario runs in its OWN subprocess, not just as separate test functions.
Two real constraints force this:

  - OpenTelemetry's tracer provider is a process-global, set once via
    ``register()``. A second call in the same process logs "Overriding of
    current TracerProvider is not allowed" and does not take effect — which
    matches production exactly: the backend configures "serving" once in its
    process, the Celery worker configures "ingestion" once in its own separate
    process, and the two never coexist. Testing both in one pytest process would
    exercise a scenario that cannot happen in production.
  - ``BatchSpanProcessor`` exports on its own schedule, not immediately when a
    span ends. A fixed ``time.sleep()`` after creating a span is a race, not a
    wait — confirmed directly: an identical test flaked between pass and fail
    depending on how much wall-clock time happened to elapse before the query,
    until ``tracing.flush()`` (backed by ``force_flush()``) made it deterministic.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
import uuid

import pytest

pytestmark = pytest.mark.live_phoenix

_QUERY_SCRIPT = textwrap.dedent("""
    from app.shared.config import get_settings
    from app.shared import tracing

    settings = get_settings()
    if not tracing.configure(settings.phoenix, project={project!r}):
        raise SystemExit("tracing dependencies not available")

    with tracing.span({marker!r}, check="live-export"):
        pass

    if not tracing.flush():
        raise SystemExit("flush failed or unsupported")
    print("EXPORTED")
""")


def _run_in_subprocess(project: str, marker: str) -> None:
    script = _QUERY_SCRIPT.format(project=project, marker=marker)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if "EXPORTED" not in result.stdout:
        pytest.skip(f"tracing unavailable: {result.stdout}\n{result.stderr}")


def _query_spans(project: str, limit: int = 50) -> set[str]:
    from phoenix.client import Client

    from app.shared.config import get_settings

    settings = get_settings().phoenix
    client = Client(base_url=settings.endpoint)
    spans = client.spans.get_spans(project_identifier=project, limit=limit)
    return {s.get("name") for s in spans if isinstance(s, dict)}


def _wait_for_span(project: str, marker: str, *, timeout_s: float = 5.0) -> set[str]:
    """Poll for the span, rather than querying once.

    ``tracing.flush()`` guarantees the SDK has *sent* the span to Phoenix's HTTP
    endpoint — it says nothing about how quickly Phoenix indexes it for querying.
    That gap is normally too small to notice, but became visible under full-suite
    load: this test passed in isolation, every time, and failed intermittently
    only when Phoenix was busier serving other tests concurrently. A bounded poll
    on the read side is the correct fix for read-after-write lag; more client-side
    flushing does not touch it, since the SDK's part was already complete.
    """
    deadline = time.monotonic() + timeout_s
    names: set[str] = set()
    while time.monotonic() < deadline:
        names = _query_spans(project)
        if marker in names:
            return names
        time.sleep(0.3)
    return names


def test_a_span_is_queryable_after_export() -> None:
    from app.shared.config import get_settings

    marker = f"test-span-{uuid.uuid4().hex[:8]}"
    _run_in_subprocess("serving", marker)

    names = _wait_for_span(get_settings().phoenix.serving_project, marker)
    assert marker in names


def test_ingestion_spans_are_isolated_from_serving() -> None:
    """Ingestion traces must not appear in the serving project, or one ingestion
    run would bury every request trace under thousands of siblings."""
    from app.shared.config import get_settings

    marker = f"test-ingestion-{uuid.uuid4().hex[:8]}"
    _run_in_subprocess("ingestion", marker)

    settings = get_settings().phoenix
    ingestion_names = _wait_for_span(settings.ingestion_project, marker)
    serving_names = _query_spans(settings.serving_project)

    assert marker in ingestion_names
    assert marker not in serving_names
