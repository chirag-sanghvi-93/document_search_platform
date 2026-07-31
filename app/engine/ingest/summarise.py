"""Document summarisation — one model call per document, skipped entirely when
the operator supplied a description.

Operator-supplied metadata is authoritative; generation is the fallback. See
doc/components/11-fastapi.md §4 and doc/components/03-contextual-agentic-rag.md
§"The document summary".
"""

from __future__ import annotations

import httpx

from app.shared.config import OllamaSettings
from app.shared.prompts import PromptSet


async def summarise_document(
    *,
    title: str,
    heading_tree: str,
    first_pages_text: str,
    description: str | None,
    prompts: PromptSet,
    ollama_settings: OllamaSettings,
) -> str:
    """Returns the document summary to store.

    If `description` was supplied, it IS the summary — no model call is made.
    This is the one place in ingestion where a stage is skipped outright rather
    than degraded, because there is nothing to improve on: an operator's own
    description cannot be improved by an inference from two pages.
    """
    if description:
        return description

    prompt = prompts["document-summarizer"]
    rendered = prompt.render(title=title, heading_tree=heading_tree, first_pages=first_pages_text)

    async with httpx.AsyncClient(
        base_url=ollama_settings.host, timeout=ollama_settings.request_timeout_s
    ) as client:
        response = await client.post(
            "/api/generate",
            json={
                "model": ollama_settings.small_model,
                "prompt": rendered,
                "stream": False,
                "options": {
                    "temperature": ollama_settings.temperature,
                    "num_ctx": ollama_settings.num_ctx,
                },
            },
        )
        response.raise_for_status()
        return str(response.json()["response"]).strip()
