"""One-time generation of fixture embeddings.

Run this ONCE, whenever `fixture_chunks.py` changes, then commit the output.
`make seed` reads the committed file and never calls Ollama — a machine without
the model pulled can still run the full test suite against a seeded database.

    uv run python -m tests.fixtures.generate_embeddings
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.shared.config import get_settings
from app.shared.models import OllamaClient
from tests.fixtures.fixture_chunks import CHUNKS

OUTPUT_PATH = Path(__file__).parent / "chunk_embeddings.json"


async def main() -> None:
    settings = get_settings()
    client = OllamaClient(settings.ollama)
    embeddings: dict[str, list[float]] = {}
    try:
        for chunk in CHUNKS:
            vector = await client.embed(chunk.embedding_text)
            if len(vector) != settings.ollama.embedding_dimension:
                raise RuntimeError(
                    f"{chunk.chunk_id}: got {len(vector)} dims, "
                    f"expected {settings.ollama.embedding_dimension}"
                )
            embeddings[chunk.chunk_id] = vector
            print(f"embedded {chunk.chunk_id} ({len(vector)} dims)")
    finally:
        await client.aclose()

    OUTPUT_PATH.write_text(json.dumps(embeddings))
    print(f"\nwrote {len(embeddings)} embeddings to {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
