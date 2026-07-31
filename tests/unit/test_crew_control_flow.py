"""Control flow through Crew.AI — no network, no database, no model.

⚠️ The point of this file is that **the split of control flow must be identical
on both paths**. Crew.AI changes who talks to the model; it must not change who
decides what happens next.

`doc/components/07-crewai.md` §4 settles that split: the application owns the
short-circuits and the fan-out, the retrieval agent owns its retry loop. If
adopting the framework quietly moved a branch inside it, every answer would still
look fine and the design would no longer be true.

`crew.run_task` is patched, which is the single seam every agent goes through.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.engine.query import crew, pipeline
from app.engine.query.corpus import CorpusDescription
from app.engine.query.search import SearchResult
from app.shared.config import Settings
from app.shared.prompts import PromptSet, ResolvedPrompt
from app.shared.types import Passage

_PROMPT_NAMES = ("planner", "retrieval-specialist", "synthesizer", "verifier")


@pytest.fixture
def prompts() -> PromptSet:
    return PromptSet(
        {
            name: ResolvedPrompt(name=name, version="test", template=name, source="bundled")
            for name in _PROMPT_NAMES
        }
    )


def _settings() -> Settings:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    settings.agents.use_crewai = True
    return settings


class _FakeClient:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    async def embed(self, _text: str) -> list[float]:
        return [0.0]

    async def aclose(self) -> None:
        return None


def _passage(chunk_id: str, score: float = 0.9) -> Passage:
    return Passage(
        chunk_id=chunk_id,
        doc_id="d1",
        source_file="f.pdf",
        title="T",
        page=1,
        display_text="A clause.",
        score=score,
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the one seam every crew agent goes through, plus search and corpus."""

    def _install(replies: dict[str, str]) -> list[str]:
        seen: list[str] = []

        def fake_run_task(agent: Any, description: str, expected_output: str) -> str:
            seen.append(agent.role)
            # The retrieval specialist is the only agent with a tool, and the
            # passages it gathers reach the pipeline through that tool rather
            # than through its prose. A fake that skipped the call would leave
            # the pipeline with nothing to synthesise — which is a different
            # code path from the one under test.
            for tool in agent.tools or []:
                tool.run(query="a search")
            for key, reply in replies.items():
                if key in agent.role.lower():
                    return reply
            raise AssertionError(f"unscripted agent: {agent.role}")

        monkeypatch.setattr(crew, "run_task", fake_run_task)
        monkeypatch.setattr(
            pipeline,
            "get_corpus_description",
            lambda *_a, **_k: CorpusDescription(
                collection="c", text="This corpus covers: baggage rules.", document_count=1
            ),
        )
        fake_search = lambda *_a, **_k: SearchResult(passages=[_passage("c1")])  # noqa: E731
        monkeypatch.setattr(pipeline, "search", fake_search)
        # ⚠️ crew.py does `from ...search import search`, so the name lives in
        # ITS module namespace. Patching only the pipeline's copy leaves the
        # tool calling the real database.
        monkeypatch.setattr(crew, "search", fake_search)
        monkeypatch.setattr("app.shared.models.OllamaClient", _FakeClient)
        return seen

    return _install


def _plan(intent: str, subs: list[str]) -> str:
    return json.dumps({"intent": intent, "standalone_question": "q?", "sub_questions": subs})


async def _run(question: str, prompts: PromptSet) -> pipeline.AnswerResult:
    return await pipeline.answer_question(
        None,  # type: ignore[arg-type]
        question,
        settings=_settings(),
        prompts=prompts,
        collection="c",
    )


# --------------------------------------------------------- the short-circuits


async def test_out_of_scope_never_reaches_an_agent_beyond_the_planner(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ The branch belongs to the APPLICATION, not to a crew process.

    If adopting the framework moved this decision inside a sequential crew, an
    out-of-scope question would run all four agents and cost four times as much
    to refuse — while every answer still looked correct."""
    seen = wired({"planner": _plan("out_of_scope", [])})

    result = await _run("how do I bake sourdough bread?", prompts)

    assert seen == ["Search planner"], "no agent may run after an out-of-scope plan"
    assert result.model_calls == 1
    assert result.searches_used == 0
    assert result.provenance == "declined"


async def test_a_capability_question_costs_one_call_and_no_search(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ The branch that a five-minute refusal paid for.

    "What can I ask you?" was planned as an ordinary lookup, searched against a
    corpus of baggage rules, found nothing, and refused after five minutes with
    three irrelevant near misses. Every stage behaved correctly; the question was
    never a retrieval question. The corpus description is already loaded to judge
    scope, so answering from it costs nothing more.
    """
    seen = wired({"planner": _plan("capability", [])})

    result = await _run("what type of questions can I ask you?", prompts)

    assert seen == ["Search planner"]
    assert result.model_calls == 1
    assert result.searches_used == 0
    assert result.passages == []
    assert "baggage rules" in result.answer, "the answer must describe the actual corpus"
    assert result.near_misses == (), "a capability answer must not offer 'closest matches'"


# ------------------------------------------------------------- the fan-out


async def test_the_application_still_owns_the_fan_out(wired: Any, prompts: PromptSet) -> None:
    """Two sub-questions means the retrieval agent runs twice, driven by this
    module — not one agent asked to handle both."""
    seen = wired(
        {
            "planner": _plan("comparison", ["a?", "b?"]),
            "retrieval": "found enough",
            "answer writer": "An answer [1].",
            "verifier": json.dumps({"verified_answer": "An answer [1].", "claims_retracted": 0}),
        }
    )

    await _run("compare a and b", prompts)

    assert seen.count("Retrieval specialist") == 2


# ---------------------------------------------------------- degradation


async def test_a_crew_failure_degrades_and_is_recorded(wired: Any, prompts: PromptSet) -> None:
    """Fail-forward is only acceptable when the failure is recorded — the same
    rule the direct path follows, and it must survive the framework."""
    wired({"planner": "not json at all"})

    result = await _run("what is the charge?", prompts)

    assert result.plan.intent == "lookup"
    assert any("planner" in reason for reason in result.degradations)


async def test_the_verifier_is_a_separate_agent_from_the_synthesizer(
    wired: Any, prompts: PromptSet
) -> None:
    """⚠️ The constraint that fixes the agent count at four.

    An agent carries its own context. Sharing one between drafting and checking
    means the verifier sees the reasoning that produced the draft, and a model
    tends to accept a claim it has just justified.
    """
    seen = wired(
        {
            "planner": _plan("lookup", ["a?"]),
            "retrieval": "found enough",
            "answer writer": "An answer [1].",
            "verifier": json.dumps({"verified_answer": "An answer [1].", "claims_retracted": 0}),
        }
    )

    await _run("what is the charge?", prompts)

    assert "Answer writer" in seen
    assert "Claims verifier" in seen
    assert seen.index("Answer writer") < seen.index("Claims verifier")
