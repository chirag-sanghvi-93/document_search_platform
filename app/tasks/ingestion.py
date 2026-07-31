"""The ingestion task.

⚠️ A thin wrapper, deliberately. It owns no logic — it opens an event loop and
calls `ingest_collection`, the same function the CLI calls. The engine never
imports Celery and never imports FastAPI, so the CLI and this task are two
callers of one implementation rather than two implementations that have to be
kept in step.

Progress is not reported through Celery. There is no result backend, and the
`ingestion_runs` table already records what a run is doing for reproducibility —
so reading progress from Postgres means there is structurally never a second,
competing account of it.

See doc/components/11-fastapi.md §5.
"""

from __future__ import annotations

import asyncio
import logging

from app.engine.ingest.pipeline import DocumentMetadata, ingest_collection
from app.shared.config import get_settings
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="ingest_document", queue="ingestion")  # type: ignore[untyped-decorator]
def ingest_document(
    collection: str,
    source_file: str,
    run_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    """Ingest ONE uploaded file into `collection`.

    ⚠️ `only=[source_file]` is what keeps this proportionate. Without it every
    upload would re-ingest the entire corpus — hours of model work to index a
    single document — and the second upload would do it again.

    `run_id` comes from the endpoint, which created the run record synchronously
    so it could return the id in its 202. Generating a new one here would leave
    the caller polling a run that is never written.
    """
    settings = get_settings()
    typed: DocumentMetadata = dict(metadata or {})  # type: ignore[assignment]

    logger.info("ingesting %s into %s (run %s)", source_file, collection, run_id)
    run = asyncio.run(
        ingest_collection(
            settings,
            collection,
            settings.ingestion.raw_dir,
            document_metadata={source_file: typed},
            only=[source_file],
            run_id=run_id,
        )
    )
    logger.info(
        "run %s finished status=%s parsed=%d chunks=%d",
        run_id,
        run.status,
        run.documents_parsed,
        run.chunks_created,
    )
