"""Prompt registry: resolution order, per-request pinning, and degradation.

These run without Phoenix. That is the point — the bundled path must work when the
registry is unreachable, so it must be testable when the registry is absent.
"""

from __future__ import annotations

import pytest

from app.shared.config import PhoenixSettings
from app.shared.prompts import (
    REQUIRED_PROMPTS,
    PromptError,
    PromptRegistry,
    PromptSet,
    ResolvedPrompt,
    load_bundled,
)


@pytest.fixture
def settings() -> PhoenixSettings:
    return PhoenixSettings()


def test_all_seven_bundled_prompts_exist(settings: PhoenixSettings) -> None:
    bundled = load_bundled(settings.prompt_dir)
    assert set(bundled) == set(REQUIRED_PROMPTS)
    assert len(bundled) == 7


def test_bundled_prompts_carry_version_and_body(settings: PhoenixSettings) -> None:
    for name, prompt in load_bundled(settings.prompt_dir).items():
        assert prompt.name == name
        assert prompt.version, f"{name} has no version"
        assert len(prompt.template) > 100, f"{name} looks empty"
        assert prompt.source == "bundled"


def test_resolution_degrades_to_bundled_without_a_registry(
    settings: PhoenixSettings,
) -> None:
    """Phoenix absent -> every prompt still resolves, and says where it came from."""
    registry = PromptRegistry(settings)
    registry._client = None  # simulate an unreachable registry

    resolved = registry.resolve_all()

    assert set(resolved.versions) == set(REQUIRED_PROMPTS)
    assert set(resolved.sources.values()) == {"bundled"}
    assert resolved.degraded is True, "serving bundled prompts must be visible, not silent"


def test_versions_are_recorded_for_every_prompt(settings: PhoenixSettings) -> None:
    """What gets written to the root span and stored with the turn."""
    registry = PromptRegistry(settings)
    registry._client = None
    versions = registry.resolve_all().versions

    assert len(versions) == 7
    assert all(isinstance(v, str) and v for v in versions.values())


def test_a_resolved_set_is_pinned_and_immutable() -> None:
    """A request holds one fixed set; later refreshes cannot alter it mid-flight."""
    pinned = PromptSet(prompts={"planner": ResolvedPrompt("planner", "3", "body", "registry")})
    assert pinned["planner"].version == "3"
    with pytest.raises(PromptError):
        _ = pinned["verifier"]


def test_render_survives_literal_braces() -> None:
    """Several prompts contain JSON examples; str.format would break on them."""
    prompt = ResolvedPrompt(
        name="verifier",
        version="1",
        template='Return {"a": 1} for {question}',
        source="bundled",
    )
    assert prompt.render(question="why?") == 'Return {"a": 1} for why?'


def test_planner_prompt_biases_toward_retrieving(settings: PhoenixSettings) -> None:
    """A wrong 'out of scope' refuses a legitimate question — the worse error."""
    planner = load_bundled(settings.prompt_dir)["planner"]
    assert "out_of_scope" in planner.template
    assert "prefer to retrieve" in planner.template.lower()


def test_synthesizer_forbids_uncited_claims(settings: PhoenixSettings) -> None:
    synthesizer = load_bundled(settings.prompt_dir)["synthesizer"]
    assert "NEVER cite a number that does not appear" in synthesizer.template
    assert "NOT a source" in synthesizer.template, "memory must not be usable as evidence"


def test_conversation_summarizer_retains_declined_questions(
    settings: PhoenixSettings,
) -> None:
    """Dropping them makes the system re-attempt the same unanswerable question."""
    summarizer = load_bundled(settings.prompt_dir)["conversation-summarizer"]
    assert "declined_unanswered" in summarizer.template
