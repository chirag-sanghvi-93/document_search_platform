"""Prompt registry against a real Phoenix instance.

These exist because the unit tests only exercise the bundled/fallback path — the
part that runs with no client at all. The methods that actually talk to
``phoenix.client`` (``_create``, ``_exists``, ``_fetch``) were written against
assumed API shapes that turned out to be wrong in two ways, both caught only by
running them for real:

  - ``create()`` requires a typed ``PromptVersion`` built from chat messages, not
    a raw string
  - a freshly created version is UNTAGGED — ``get(tag=...)`` never finds it
    without a separate ``tags.create()`` call, which silently broke idempotency
    (every "push" created 7 new versions, forever)

A mock would not have caught the second one: it would have verified `create` was
*called*, not that the result becomes *findable*. Only a real round trip does.
"""

from __future__ import annotations

import pytest

from app.shared.config import get_settings
from app.shared.prompts import (
    REQUIRED_PROMPTS,
    PromptRegistry,
    ResolvedPrompt,
    load_bundled,
)

pytestmark = pytest.mark.live_phoenix


@pytest.fixture
def registry() -> PromptRegistry:
    settings = get_settings().phoenix
    reg = PromptRegistry(settings)
    if reg._client is None:
        pytest.skip("phoenix client not available — is arize-phoenix-client installed?")
    return reg


def test_push_then_repush_is_idempotent(registry: PromptRegistry) -> None:
    """The second push must create zero new versions.

    This is the assertion that catches the untagged-version bug: with it missing,
    `_exists` always returns False and this would report 7, not 0, every time.
    """
    registry.push_bundled()  # ensure at least one exists
    created = registry.push_bundled()
    assert created == 0


def test_resolved_text_matches_bundled_files_exactly(registry: PromptRegistry) -> None:
    """The round-trip recovery technique must not corrupt the template.

    Verifies the workaround for the missing `.template` property: feeding each
    variable its own placeholder name back through `.format()` must reproduce the
    original text byte-for-byte, for every real prompt file, not just a
    synthetic example.
    """
    registry.push_bundled()
    settings = get_settings().phoenix
    bundled = load_bundled(settings.prompt_dir)

    resolved = registry.resolve_all()

    assert set(resolved.versions) == set(REQUIRED_PROMPTS)
    assert resolved.degraded is False, "expected every prompt to resolve from the live registry"

    for name in REQUIRED_PROMPTS:
        assert resolved[name].source == "registry"
        assert resolved[name].template.strip() == bundled[name].template.strip(), (
            f"'{name}' did not round-trip exactly through Phoenix"
        )


def test_an_edited_prompt_is_pushed_as_a_new_version(registry: PromptRegistry) -> None:
    """⚠️ Regression: edits to a bundled prompt never reached the registry.

    `push_bundled` originally compared EXISTENCE, not content — so once a prompt
    was pushed under the tag, every later edit was skipped. The registry served
    the first version forever while the file on disk showed the new one, and the
    system kept answering with the old wording. Editing a prompt appeared to do
    nothing, which defeats the point of externalising them.

    Caught in practice: a planner prompt rewritten to stop it over-decomposing
    was silently never applied, and a subsequent measurement was attributed to
    the wrong cause.
    """
    registry.push_bundled()
    assert registry.push_bundled() == 0, "a second push of unchanged prompts must be a no-op"

    name = "planner"
    original = registry._bundled[name]
    edited = ResolvedPrompt(
        name=original.name,
        version=original.version,
        template=original.template + "\n- A rule appended by the test.",
        source="bundled",
        variables=original.variables,
    )
    registry._bundled[name] = edited
    try:
        assert registry.push_bundled() == 1, "an edited prompt must be pushed"
        live = registry.resolve_all()[name].template
        assert "A rule appended by the test." in live
    finally:
        # Restore, and push the original back so the registry matches disk again.
        registry._bundled[name] = original
        registry.push_bundled()

    restored = PromptRegistry(get_settings().phoenix).resolve_all()[name].template
    assert "A rule appended by the test." not in restored
