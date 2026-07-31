"""The Crew.AI layer: four agents, one tool, and the split with the application.

⚠️ **What this module owns, and what it deliberately does not.**

`doc/components/07-crewai.md` §4 settles the control-flow question, and the split
is not negotiable because a small model cannot be trusted with the other half:

    the APPLICATION owns   the out-of-scope short-circuit (so it costs nothing)
                           and the fan-out across sub-questions
    the AGENT owns         its own retry loop, bounded by max_iter, calling one
                           tool repeatedly until the evidence is sufficient

Hierarchical delegation — a manager agent deciding who runs — was rejected in the
design because it depends on delegation reliability an 8B-class model does not
have. Every agent here therefore has `allow_delegation=False`.

**Four agents, not three.** Synthesis and verification look like one job. They
cannot share an agent, because an agent carries its own context: combining them
means the verifier sees the reasoning that produced the draft, and a model tends
to accept a claim it has just justified. That constraint alone fixes the count.

**One tool.** The retrieval specialist gets `search(query)` and nothing else.
Tool-selection error is the dominant failure mode at this model size, so one tool
called repeatedly with different queries is far more reliable than four tools
called once each.

⚠️ **Roles and goals come from the prompt registry, not from code** —
requirement 3.3. The same `PromptSet` the hand-rolled path uses is what fills the
agents' goals here, so a prompt edit changes both without a redeploy.

⚠️ **Framework token overhead is real and is a cost, not a rounding error.**
Role, goal, backstory and task scaffolding are prepended to every call and
compete directly with the 8192-token window that also has to hold instructions,
evidence, memory and generation headroom. Measured against the direct path, this
is why `AgentSettings.use_crewai` exists — see the note there.
"""

from __future__ import annotations

import logging
import os
from typing import Any

# ⚠️ Set BEFORE crewai is imported. The framework prints a tracing-consent banner
# to stdout on first use and phones home unless told not to — neither of which
# belongs in a system that runs entirely on local models over a corpus that may
# be confidential.
os.environ.setdefault("CREWAI_TRACING_ENABLED", "false")
os.environ.setdefault("CREWAI_TELEMETRY_OPT_OUT", "true")

# ⚠️ NOT `OTEL_SDK_DISABLED`. That was the first attempt and it silenced the
# framework's telemetry by turning OpenTelemetry off process-wide — which also
# disabled OUR Phoenix tracing, the whole of baseline item 8. Nothing failed
# visibly; two live tracing tests went red and were the only sign. Opt out of
# CrewAI specifically, never of the SDK everything else depends on.

from crewai import LLM, Agent, Crew, Process, Task
from crewai.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.engine.query.retrieval import SearchFilters
from app.engine.query.search import search
from app.shared.config import OllamaSettings, RetrievalSettings
from app.shared.prompts import PromptSet
from app.shared.types import Passage

logger = logging.getLogger(__name__)


def build_llm(model: str, settings: OllamaSettings) -> LLM:
    """One LLM handle per model.

    `ollama/` prefix so litellm routes to the native host rather than assuming a
    hosted provider. Temperature is pinned at zero everywhere for the same reason
    it is elsewhere: determinism is worth more than variety in a cited system.
    """
    return LLM(
        model=f"ollama/{model}",
        base_url=settings.host,
        temperature=settings.temperature,
        timeout=settings.request_timeout_s,
    )


class SearchInput(BaseModel):
    query: str = Field(description="A self-contained search question.")


class SearchTool(BaseTool):
    """The ONE tool. Wraps the existing search engine — it does not reimplement it.

    ⚠️ Returns `display_text` only, exactly as `_format_passages` does on the
    direct path. `embedding_text` carries a model-written contextual preamble,
    and letting an agent see it would put machine-generated wording in line to be
    quoted under a page citation.

    Passages retrieved during the agent's loop are accumulated on the tool rather
    than parsed back out of its string output. The framework hands the agent a
    string; the application needs `Passage` objects with scores and metadata, and
    re-parsing prose to recover them would be inventing a fragile channel where a
    reference already exists.
    """

    name: str = "search"
    description: str = (
        "Search the document corpus for passages relevant to a question. "
        "Call it again with a reworded query if the results are insufficient."
    )
    args_schema: type[BaseModel] = SearchInput

    model_config = ConfigDict(arbitrary_types_allowed=True)

    session: Any = None
    embedding: Any = None
    settings: Any = None
    filters: Any = None
    keep: int = 5
    # default_factory, not a shared list: a mutable class default would
    # accumulate passages across every question the process ever answers.
    collected: list[Passage] = Field(default_factory=list)
    calls: int = 0
    budget: int = 6

    def _run(self, query: str) -> str:
        if self.calls >= self.budget:
            # ⚠️ A structured refusal, not an exception. The agent can act on a
            # message; an exception ends the task and discards work already done.
            return "SEARCH BUDGET EXHAUSTED. Answer from the passages you already have."

        self.calls += 1
        result = search(
            self.session, query, self.embedding, self.settings, self.filters, keep=self.keep
        )
        self.collected.extend(result.passages)

        if not result.passages:
            return "No passages cleared the relevance threshold for that query."

        return "\n\n".join(f"[{i}] {p.display_text}" for i, p in enumerate(result.passages, 1))


def _agent(role: str, goal: str, backstory: str, llm: LLM, **kwargs: Any) -> Agent:
    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        llm=llm,
        # ⚠️ Never delegate: the design rejected hierarchical control on the
        # grounds that a model this size cannot be relied on to route work.
        allow_delegation=False,
        verbose=False,
        **kwargs,
    )


def planner_agent(prompts: PromptSet, llm: LLM) -> Agent:
    return _agent(
        role="Search planner",
        goal=prompts["planner"].template[:400],
        backstory=(
            "You decide how a question should be answered from a fixed corpus: "
            "whether it is in scope at all, how to phrase it so it stands alone, "
            "and whether it needs to be split."
        ),
        llm=llm,
    )


def retrieval_agent(prompts: PromptSet, llm: LLM, tool: SearchTool, max_iter: int) -> Agent:
    return _agent(
        role="Retrieval specialist",
        goal=prompts["retrieval-specialist"].template[:400],
        backstory=(
            "You find the passages that answer a question. If the first search "
            "is not enough, you reword the query and search again — but you stop "
            "as soon as the evidence is sufficient."
        ),
        llm=llm,
        tools=[tool],
        # The retry bound is a configured limit rather than hand-written loop
        # counting: this is precisely the half of control flow the agent owns.
        max_iter=max_iter,
    )


def synthesizer_agent(prompts: PromptSet, llm: LLM) -> Agent:
    return _agent(
        role="Answer writer",
        goal=prompts["synthesizer"].template[:400],
        backstory=(
            "You write answers grounded strictly in the passages you are given, "
            "marking every factual claim with the number of its source."
        ),
        llm=llm,
    )


def verifier_agent(prompts: PromptSet, llm: LLM) -> Agent:
    return _agent(
        role="Claims verifier",
        goal=prompts["verifier"].template[:400],
        backstory=(
            "You check a drafted answer against the evidence and remove any "
            "claim the evidence does not support. You never see the reasoning "
            "that produced the draft, only the draft and the passages."
        ),
        llm=llm,
    )


def run_task(agent: Agent, description: str, expected_output: str) -> str:
    """One agent, one task, one crew — run synchronously.

    A crew per task rather than one sequential crew for all four is deliberate:
    the application decides what happens between them (short-circuit, fan-out,
    deduplication), and a sequential process would take those decisions away
    while providing nothing this system needs in return.
    """
    task = Task(description=description, expected_output=expected_output, agent=agent)
    crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
    return str(crew.kickoff())


def make_search_tool(
    session: Session,
    embedding: list[float],
    settings: RetrievalSettings,
    filters: SearchFilters,
    *,
    keep: int,
    budget: int,
) -> SearchTool:
    tool = SearchTool()
    tool.session = session
    tool.embedding = embedding
    tool.settings = settings
    tool.filters = filters
    tool.keep = keep
    tool.budget = budget
    # Fresh list per request: a class-level default would accumulate passages
    # across every question the process ever answers.
    tool.collected = []
    tool.calls = 0
    return tool
