"""Ingestion CLI.

    make ingest COLLECTION=default

Runs the same pipeline the (future) upload endpoint's Celery task calls — the
engine never imports the web framework, so this and that task are both thin
callers of `ingest_collection`, not two different implementations. See
doc/components/11-fastapi.md §7.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.engine.ingest.pipeline import ingest_collection
from app.shared.config import get_settings

logger = logging.getLogger(__name__)


async def _main(collection: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    settings = get_settings()

    run = await ingest_collection(settings, collection, settings.ingestion.raw_dir)

    print(
        f"collection={run.collection} status={run.status} "
        f"seen={run.documents_seen} parsed={run.documents_parsed} "
        f"skipped={run.documents_skipped} chunks={run.chunks_created} "
        f"preambles={run.preambles_generated}"
    )
    return 0 if run.status == "completed" else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest PDFs from data/raw into a collection")
    parser.add_argument("--collection", default="default")
    args = parser.parse_args()

    sys.exit(asyncio.run(_main(args.collection)))


if __name__ == "__main__":
    main()
