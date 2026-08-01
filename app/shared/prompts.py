"""Prompt registry.

Satisfies the three parts of the prompt requirement:

  (a) prompts are initialised IN the registry   -> :meth:`PromptRegistry.push_bundled`
  (b) prompts are retrieved FROM it at runtime  -> :meth:`PromptRegistry.resolve_all`
  (c) which version produced an answer is recorded -> :attr:`PromptSet.versions`

Two properties carry most of the design weight:

**Resolution is pinned per request.** All prompts are resolved once, at the start of a
request, and that set is passed down. A cache refreshing mid-request would produce an
answer from a combination of versions that never existed as a set — untraceable, and
unreproducible from the recorded version numbers.

**Failure degrades to bundled files, and says so.** If the registry is unreachable the
system keeps answering from ``prompts/``. Because that is invisible by design, every
resolution records its ``source``, so a deployment silently serving bundled prompts on
every request is detectable rather than merely survivable.

See doc/components/08-arize-phoenix.md — the authority on prompts and trace design.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from app.shared.config import PhoenixSettings

logger = logging.getLogger(__name__)

PromptSource = Literal["registry", "cache", "bundled"]

#: Every prompt the system needs. Resolution fails loudly if one is missing, rather
#: than discovering it mid-request when the relevant agent first runs.
REQUIRED_PROMPTS = (
    "chunk-contextualizer",
    "document-summarizer",
    "conversation-summarizer",
    "planner",
    "retrieval-specialist",
    "synthesizer",
    "verifier",
)

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class PromptError(RuntimeError):
    """Raised when a prompt cannot be resolved from any source."""


@dataclass(frozen=True)
class ResolvedPrompt:
    name: str
    version: str
    template: str
    source: PromptSource
    variables: tuple[str, ...] = ()
    #: One line, from the bundled file's frontmatter. Pushed to Phoenix as the
    #: PROMPT-level description — the list view's "description" column reads
    #: this, not the per-version description below.
    description: str = ""

    def render(self, **variables: object) -> str:
        """Fill the template.

        Uses ``str.replace`` rather than ``str.format`` deliberately: prompt text
        contains literal braces (JSON examples, especially in the planner and
        verifier), and ``format`` would raise on them or silently consume them.
        """
        text = self.template
        for key, value in variables.items():
            text = text.replace("{" + key + "}", str(value))
        return text


@dataclass(frozen=True)
class PromptSet:
    """The prompts resolved for one request, pinned for its lifetime."""

    prompts: dict[str, ResolvedPrompt] = field(default_factory=dict)

    def __getitem__(self, name: str) -> ResolvedPrompt:
        try:
            return self.prompts[name]
        except KeyError:
            raise PromptError(f"prompt '{name}' was not resolved for this request") from None

    @property
    def versions(self) -> dict[str, str]:
        """Recorded on the root span and stored with the turn."""
        return {name: p.version for name, p in self.prompts.items()}

    @property
    def sources(self) -> dict[str, PromptSource]:
        return {name: p.source for name, p in self.prompts.items()}

    @property
    def degraded(self) -> bool:
        """True when any prompt came from somewhere other than the live registry.

        The signal that makes fail-forward visible.
        """
        return any(p.source != "registry" for p in self.prompts.values())


def _parse_bundled(path: Path) -> ResolvedPrompt:
    raw = path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(raw)
    if match is None:
        raise PromptError(f"{path.name} has no frontmatter block")

    meta, body = match.group(1), match.group(2).strip()
    fields: dict[str, str] = {}
    for line in meta.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    name = fields.get("name") or path.stem
    raw_vars = fields.get("variables", "").strip("[]")
    variables = tuple(v.strip() for v in raw_vars.split(",") if v.strip())

    return ResolvedPrompt(
        name=name,
        version=fields.get("version", "0"),
        template=body,
        source="bundled",
        variables=variables,
        description=fields.get("description", ""),
    )


def load_bundled(prompt_dir: Path) -> dict[str, ResolvedPrompt]:
    """Read the bundled prompt files. These are the fallback, never the truth."""
    loaded: dict[str, ResolvedPrompt] = {}
    for name in REQUIRED_PROMPTS:
        path = prompt_dir / f"{name}.md"
        if not path.exists():
            raise PromptError(f"bundled prompt missing: {path}")
        loaded[name] = _parse_bundled(path)
    return loaded


class PromptRegistry:
    """Resolves prompts, preferring the registry and degrading to bundled files."""

    def __init__(self, settings: PhoenixSettings) -> None:
        self._settings = settings
        self._bundled = load_bundled(settings.prompt_dir)
        self._cache: dict[str, ResolvedPrompt] = {}
        self._cached_at: float = 0.0
        self._client = self._make_client()

    def _make_client(self) -> object | None:
        """The client is optional at import time.

        Without it — dependency absent, or the server unreachable — the system runs
        entirely on bundled prompts. That is the documented degradation, so an
        unavailable client must not be an error.
        """
        try:
            from phoenix.client import Client
        except ImportError:
            logger.warning(
                "phoenix client not installed; prompts will resolve from bundled files only"
            )
            return None
        try:
            client: object = Client(base_url=self._settings.endpoint)
        except Exception as exc:
            logger.warning("phoenix client unavailable (%s); using bundled prompts", exc)
            return None
        return client

    # -- (a) initialise prompts IN the registry ----------------------------------

    def push_bundled(self) -> int:
        """Push every bundled prompt, idempotently. Returns the number newly created.

        ⚠️ Idempotence is what keeps the version history meaningful. A startup push
        that creates a version every time turns the history — the reason to have a
        registry at all — into noise within a week.

        The comparison is on CONTENT, not mere existence — using the same
        round-trip recovery `_fetch` relies on, since ``PromptVersion`` exposes
        no public template property.

        ⚠️ An earlier version compared existence only, and that was a real bug:
        once a prompt existed under the tag, edits to the bundled file were
        **never** pushed. The registry silently kept serving the first version
        forever, so editing a prompt appeared to do nothing — and the system went
        on answering with the old wording while the file on disk showed the new
        one. That defeats the point of externalised prompts.

        (The previous docstring claimed bumping the ``version:`` frontmatter
        would force a new version. It does not: the tag is fixed at
        ``prompt_tag`` and does not vary with frontmatter.)
        """
        if self._client is None:
            logger.info("no registry client; skipping prompt push")
            return 0

        created = 0
        for name, prompt in self._bundled.items():
            try:
                remote = self._fetch(name)
                if remote is not None and remote.template.strip() == prompt.template.strip():
                    continue  # unchanged — do not create a redundant version
                self._create(name, prompt)
                created += 1
                if remote is not None:
                    logger.info("prompt '%s' changed on disk; pushed a new version", name)
            except Exception as exc:
                logger.warning("could not push prompt '%s': %s", name, exc)

        logger.info(
            "prompt push complete: %d created/updated, %d unchanged",
            created,
            len(self._bundled) - created,
        )
        return created

    def _exists(self, name: str) -> bool:
        client = self._client
        assert client is not None
        try:
            client.prompts.get(prompt_identifier=name, tag=self._settings.prompt_tag)  # type: ignore[attr-defined]
        except Exception:
            return False
        return True

    def _create(self, name: str, prompt: ResolvedPrompt) -> None:
        """Create a version, then tag it.

        ⚠️ These are two separate calls, confirmed against the live API — a fresh
        version is untagged. Without the second call, ``get(tag=...)`` never finds
        it, ``_exists`` always returns False, and every startup "pushes" the same
        seven prompts as new versions forever. Caught by actually running this
        twice against Phoenix and checking the created count was zero the second
        time — a mock would not have caught it, since the mock would only assert
        that ``create`` was called, not that the result becomes findable.
        """
        from phoenix.client.types import PromptVersion

        client = self._client
        assert client is not None
        # ⚠️ TWO description fields, at two different levels, and the list view
        # in the Phoenix UI reads the PROMPT-level one — `prompt_description`
        # below — not this per-version description. Confirmed by inspecting
        # `Prompts.create`'s real signature rather than assumed: every prompt
        # showed "--" in the description column despite each bundled file
        # carrying one, because only the version-level field was ever set.
        version = PromptVersion(
            [{"role": "user", "content": prompt.template}],
            model_name=self._settings.prompt_tag,
            model_provider="OLLAMA",
            template_format="F_STRING",
            description=prompt.description or f"Pushed from bundled prompts/{name}.md",
        )
        created = client.prompts.create(  # type: ignore[attr-defined]
            name=name,
            version=version,
            prompt_description=prompt.description or None,
        )
        client.prompts.tags.create(  # type: ignore[attr-defined]
            prompt_version_id=created.id, name=self._settings.prompt_tag
        )

    def _fetch(self, name: str) -> ResolvedPrompt | None:
        """Fetch the live version and recover its raw template text.

        The public API only returns fully-substituted text via ``.format()``, and
        refuses to format unless every declared variable is supplied. The
        workaround — verified against the real client, not assumed — feeds each
        variable its own placeholder text (``{name}`` -> the literal string
        ``"{name}"``) back in as the substitution value. Since F_STRING formatting
        is a plain positional replace, the result is byte-identical to the
        original template, recovered entirely through public API surface.
        """
        client = self._client
        if client is None:
            return None
        try:
            remote = client.prompts.get(  # type: ignore[attr-defined]
                prompt_identifier=name, tag=self._settings.prompt_tag
            )
        except Exception:
            return None

        variables = self._bundled[name].variables
        roundtrip = {v: "{" + v + "}" for v in variables}
        try:
            formatted = remote.format(variables=roundtrip, sdk="openai")
            text = str(formatted.messages[-1]["content"])
        except Exception as exc:
            logger.warning("could not recover template text for '%s': %s", name, exc)
            return None

        return ResolvedPrompt(
            name=name,
            version=str(remote.id or "unknown"),
            template=text,
            source="registry",
            variables=variables,
        )

    # -- (b) retrieve prompts FROM the registry at runtime -----------------------

    def resolve_all(self) -> PromptSet:
        """Resolve every prompt once, for the lifetime of one request.

        Call this at request start and pass the result down. Resolving individually
        as each agent runs would let a mid-request cache refresh mix versions.
        """
        if self._cache_is_fresh():
            return PromptSet(prompts=dict(self._cache))

        resolved: dict[str, ResolvedPrompt] = {}
        for name in REQUIRED_PROMPTS:
            resolved[name] = self._resolve_one(name)

        self._cache = resolved
        self._cached_at = time.monotonic()
        return PromptSet(prompts=dict(resolved))

    def _cache_is_fresh(self) -> bool:
        if not self._cache:
            return False
        return (time.monotonic() - self._cached_at) < self._settings.prompt_cache_ttl_s

    def _resolve_one(self, name: str) -> ResolvedPrompt:
        """Registry -> last cached -> bundled. In that order, always."""
        remote = self._fetch(name)
        if remote is not None:
            return remote

        stale = self._cache.get(name)
        if stale is not None:
            return ResolvedPrompt(
                name=stale.name,
                version=stale.version,
                template=stale.template,
                source="cache",
            )

        return self._bundled[name]
