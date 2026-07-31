"""Embedding — the last model-calling stage before the atomic write.

Batched by the pipeline, not by this module: this function embeds whatever list
it is given, and the pipeline is what guarantees that list is "every chunk
across the whole run," not one document's worth. See pipeline.py and
doc/components/06-ollama.md — the authority on model selection.
"""

from __future__ import annotations

from app.shared.models import OllamaClient


async def embed_texts(texts: list[str], client: OllamaClient) -> list[list[float]]:
    return [await client.embed(text) for text in texts]
