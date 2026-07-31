"""Ollama client.

The model host runs natively rather than in Compose: containers on macOS cannot
reach the Metal GPU, and a CPU-only model host is unusably slow for the per-chunk
work in ingestion. Containers reach it at ``host.docker.internal``.

See doc/components/06-ollama.md — the authority on model selection.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.shared.config import OllamaSettings


class OllamaError(RuntimeError):
    """Raised when the model host is unreachable or returns an unusable response."""


@dataclass(frozen=True)
class ModelAvailability:
    """Outcome of checking that the models this system needs are actually present."""

    available: list[str]
    missing: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing


class OllamaClient:
    def __init__(self, settings: OllamaSettings, *, for_health_check: bool = False) -> None:
        """
        Args:
            for_health_check: use the short probe timeout instead of the generation
                timeout. Health checks must fail fast — a probe that inherits the
                300 s generation budget leaves ``/health/ready`` hanging for five
                minutes when the model host is unreachable, which is indistinguishable
                from the service being dead.
        """
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.host,
            timeout=(settings.health_timeout_s if for_health_check else settings.request_timeout_s),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_models(self) -> list[str]:
        """Model names present on the host, with any ``:latest`` suffix normalised away."""
        try:
            response = await self._client.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(f"Cannot reach Ollama at {self._settings.host}: {exc}") from exc

        payload = response.json()
        return [_normalise(model["name"]) for model in payload.get("models", [])]

    async def check_models(self) -> ModelAvailability:
        """Check every model this system depends on, excluding the judge.

        The judge model is used only by the offline evaluation harness, so its
        absence must not make a serving deployment look broken.
        """
        # Deduplicated because these fields are allowed to name the same model —
        # `verifier_model` and `answering_model` are both qwen2.5:7b by default,
        # and reporting it twice would make the readiness log misleading.
        required = list(
            dict.fromkeys(
                [
                    self._settings.embedding_model,
                    self._settings.small_model,
                    self._settings.contextualiser_model,
                    self._settings.answering_model,
                    self._settings.verifier_model,
                ]
            )
        )
        present = set(await self.list_models())
        available = [name for name in required if _normalise(name) in present]
        missing = [name for name in required if _normalise(name) not in present]
        return ModelAvailability(available=available, missing=missing)

    async def embed(self, text: str) -> list[float]:
        try:
            response = await self._client.post(
                "/api/embed",
                json={"model": self._settings.embedding_model, "input": text},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(f"Embedding call failed: {exc}") from exc

        embeddings = response.json().get("embeddings") or []
        if not embeddings:
            raise OllamaError("Embedding response contained no vector")
        return list(embeddings[0])

    async def check_embedding_dimension(self) -> int:
        """Return the embedding width, raising if it disagrees with configuration.

        This is asserted at startup rather than discovered later because
        ``vector(N)`` is fixed when the chunks table is created. A mismatch found
        after ingestion means dropping the column and re-embedding the corpus.
        """
        vector = await self.embed("dimension probe")
        actual = len(vector)
        expected = self._settings.embedding_dimension
        if actual != expected:
            raise OllamaError(
                f"{self._settings.embedding_model} returned {actual} dimensions, "
                f"but configuration expects {expected}. The chunks table is created "
                f"as vector({expected}); fix OLLAMA__EMBEDDING_DIMENSION before ingesting."
            )
        return actual


def _normalise(model_name: str) -> str:
    """``qwen3:8b`` and ``qwen3:8b:latest`` name the same model."""
    return model_name.removesuffix(":latest")
