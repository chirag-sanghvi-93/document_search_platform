"""Celery application.

Configuration and queue routing live here; the task definitions themselves are in
`app.tasks.ingestion`, imported below for its registration side effect.

See doc/components/11-fastapi.md §5.
"""

from __future__ import annotations

from celery import Celery

from app.shared.config import get_settings

settings = get_settings()

celery_app = Celery("document_search_platform", broker=settings.celery.broker_url)

celery_app.conf.update(
    # No result backend. Progress is read from ingestion_runs in Postgres — the
    # record that already exists for reproducibility — so there is structurally
    # never a second, competing account of what a job is doing.
    task_ignore_result=settings.celery.ignore_result,
    result_backend=None,
    task_default_queue=settings.celery.default_queue,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    timezone="UTC",
    enable_utc=True,
    # ⚠️ Routing is by task, not left to the caller. The ingestion queue runs at
    # concurrency 1 — load-bearing, not conservative: the pipeline batches by
    # model, and two concurrent runs reintroduce the model thrash that batching
    # exists to prevent, from outside the pipeline where nothing inside it can
    # detect the cause. A task that reached the default queue would quietly get
    # concurrency 2 and undo that.
    task_routes={"ingest_document": {"queue": settings.celery.ingestion_queue}},
)

# Imported for its side effect: registering the task on this app. Without it a
# worker started against `celery_app` accepts the message and rejects it as
# unregistered — which looks like a broker problem and is not.
from app.tasks import ingestion as _ingestion  # noqa: E402,F401
