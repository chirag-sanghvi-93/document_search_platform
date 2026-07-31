"""Contextual preambles — one model call per chunk, the most consequential and
most expensive stage in ingestion.

⚠️ The cache is keyed on **chunk text**, prompt version and model name —
deliberately NOT on chunk_id or position. Retuning chunk size (or anything else
upstream of chunking) changes positions but leaves most chunk TEXT unchanged, so
most preambles are still found in cache. Keying on position instead would
invalidate the entire cache on every chunking experiment — the difference
between a retuning costing minutes and costing hours. See
doc/components/03-contextual-agentic-rag.md and doc/components/02b-pgvector-postgresql.md §7.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from app.shared.config import OllamaSettings
from app.shared.prompts import PromptSet


def _cache_key(chunk_text: str, prompt_version: str, model: str) -> str:
    digest = hashlib.sha256(f"{chunk_text}\x00{prompt_version}\x00{model}".encode()).hexdigest()
    return digest


def _cache_path(preamble_dir: Path, key: str) -> Path:
    return preamble_dir / f"{key}.txt"


async def contextualise_chunk(
    *,
    document_summary: str,
    heading_path: str,
    chunk_text: str,
    prompts: PromptSet,
    ollama_settings: OllamaSettings,
    preamble_dir: Path,
) -> str:
    """Returns the situating sentence for one chunk, from cache if available.

    Uses `contextualiser_model` — a non-reasoning model, deliberately distinct
    from `small_model`. See the comment on that setting in app/shared/config.py:
    a reasoning model spent ~5,000 tokens and ~194s per chunk here, which is
    ~32 hours for one corpus.
    """
    prompt = prompts["chunk-contextualizer"]
    # The model name is part of the cache key, so switching models correctly
    # invalidates preambles produced by the previous one rather than silently
    # serving a mix of the two.
    key = _cache_key(chunk_text, prompt.version, ollama_settings.contextualiser_model)
    cached = _cache_path(preamble_dir, key)

    if cached.exists():
        return cached.read_text().strip()

    rendered = prompt.render(
        document_summary=document_summary, heading_path=heading_path, chunk_text=chunk_text
    )

    async with httpx.AsyncClient(
        base_url=ollama_settings.host, timeout=ollama_settings.request_timeout_s
    ) as client:
        response = await client.post(
            "/api/generate",
            json={
                "model": ollama_settings.contextualiser_model,
                "prompt": rendered,
                "stream": False,
                "options": {
                    "temperature": ollama_settings.temperature,
                    "num_ctx": ollama_settings.num_ctx,
                    # A situating sentence needs ~60 tokens. This is a guard, not
                    # a tuning knob: if a future model does start rambling, the
                    # cost is bounded instead of silently becoming hours.
                    "num_predict": 200,
                },
            },
        )
        response.raise_for_status()
        preamble = str(response.json()["response"]).strip()

    preamble_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(preamble)
    return preamble


def assemble_embedding_text(*, heading_path: str, preamble: str, display_text: str) -> str:
    """Heading path + preamble + original — never a substitute for
    `display_text`, only ever wrapping it. See doc/components/01-docling.md §4."""
    parts = [p for p in (heading_path, preamble, display_text) if p]
    return "\n".join(parts)
