# Observability & Prompts — Build Record

> The second phase of the build: prompts held outside the codebase, and every model call traced.
>
> This document records what was built, how each component was *verified* rather than assumed, and
> what went wrong along the way. The design documents say what should be built; this says what was.

---

## 1. What this phase covers

Satisfies constraint 3.3 (externalised prompts) and constraint 3.2 (observable), and the three-part
requirement the brief singles out explicitly:

- **(a)** prompts are initialised **in** Phoenix
- **(b)** prompts are retrieved **from** Phoenix at runtime
- **(c)** every model call is traced, for debugging and observability

| Area | Delivers | Status |
|---|---|---|
| Bundled prompts | Seven prompt files — the fallback, and the source pushed to the registry | ✅ |
| Prompt registry | Idempotent push, per-request pinning, three-tier fallback | ✅ |
| Tracing | OTLP export, separate projects for serving vs. ingestion | ✅ |
| Live verification | Both of the above proven against a running Phoenix, not mocked | ✅ |

### Explicit non-goals

- **No decision attributes yet** — there is nothing to decide until the agents exist
- **No evaluation harness** — a later phase
- **The seven prompts' final wording** — these are working drafts; each is refined by the phase that
  actually exercises it

---

## 2. Components: steps executed and how each was verified

The same rule as the previous phase: **a step ends in a demonstration, not a claim.**

### 2.1 Bundled prompts

**Steps**

```bash
mkdir -p prompts
# seven files, each with YAML frontmatter (name, version, stage, description, variables)
# followed by the prompt body
```

**Verified**

```bash
uv run pytest tests/unit/test_prompts.py -q
```

```
9 passed
```

Covers: all seven exist, each carries a version and a non-trivial body, the planner is biased toward
retrieving rather than declining, the synthesizer forbids uncited claims, the conversation summarizer
retains declined questions.

### 2.2 Dependency group

**Steps**

```toml
# pyproject.toml
observability = [
    "arize-phoenix-client>=1.0",   # the SERVER runs as a container; only the client is needed here
    "arize-phoenix-otel>=0.6",
    "openinference-instrumentation-litellm>=0.1",
    "opentelemetry-sdk>=1.29",
    "opentelemetry-exporter-otlp>=1.29",
]
```

```bash
uv sync --extra observability
```

**Verified**

```
Resolved 296 packages
Installed 23 packages, ~12 MB
  arize-phoenix-client==2.13.0
  arize-phoenix-otel==0.16.1
  opentelemetry-sdk==1.42.1
  ...
```

> Depending on the full `arize-phoenix` package would pull the server and its web stack into every
> backend and worker image, for a server we already run as a container. The client-only package avoids
> that entirely.

### 2.3 Prompt registry — push, resolve, degrade

**Steps**

```python
# app/shared/prompts.py
registry = PromptRegistry(settings.phoenix)
registry.push_bundled()  # (a) initialise in the registry
resolved = registry.resolve_all()  # (b) retrieve at runtime, pinned for the request
resolved.versions  # (c) which version answered — for the root span and the stored turn
```

Wired into application startup in `app/main.py`, inside the FastAPI lifespan.

**Verified — first against the bundled/fallback path, with no server at all:**

```bash
uv run pytest tests/unit/test_prompts.py -q
```

```
9 passed
```

**Then against a live Phoenix**, which is where the actual work of this phase was:

```bash
uv run python -c "
from app.shared.config import get_settings
from app.shared.prompts import PromptRegistry
r = PromptRegistry(get_settings().phoenix)
print('created:', r.push_bundled())
"
```

```
created: 7        # first run
created: 0         # second run — idempotency confirmed
```

```bash
uv run pytest tests/integration/test_prompts_live.py -m live_phoenix -v
```

```
test_push_then_repush_is_idempotent                        PASSED
test_resolved_text_matches_bundled_files_exactly            PASSED
```

### 2.4 Tracing — export, projects, root-span attributes

**Steps**

```python
# app/shared/tracing.py
tracing.configure(settings.phoenix, project="serving")  # or "ingestion"
with tracing.span("stage-name", **attributes):
    ...
tracing.set_root_attributes(intent="lookup", retries_used=1)
tracing.flush()  # deterministic — see §3.2
```

**Verified**

```bash
uv run pytest tests/integration/test_tracing_live.py -m live_phoenix -v
```

```
test_a_span_is_queryable_after_export                       PASSED
test_ingestion_spans_are_isolated_from_serving               PASSED
```

Both assertions query Phoenix's own API for the span back — `client.spans.get_spans(project_identifier=...)`
— rather than trusting the exporter's own "success" log line, which only confirms the SDK sent
something, not that Phoenix received and indexed it.

### 2.5 Full suite

```bash
uv run pytest -q          # everything
```

```
25 passed
```

Run **five times in a row** to confirm this is genuinely deterministic rather than passing by chance —
see §3 for why that check mattered.

---

## 3. Challenges and how they were resolved

Four defects in the client-facing code, all found only by running it — not by reasoning about the
library from memory — plus two timing bugs. None of the six would have been caught by a mock.

### 3.1 The client library's real shape differed from what was written

`prompts.py` was originally written against an assumed API. Installing the real package and inspecting
it directly (`inspect.signature`, reading source) turned up two mismatches before anything was even
run:

| Assumed | Actual |
|---|---|
| `create(name=..., version=<string>)` | `create()` needs a typed `PromptVersion` built from chat messages — `[{"role": "user", "content": text}]` — with `model_provider` (which includes `"OLLAMA"` as a first-class option) and `template_format` |
| `remote.template` readable directly | **No such public property.** The class's own `__dir__` override restricts it to `id`, `format`, `from_openai`, `from_anthropic` — deliberately hiding the raw text |

**Fix for the second one is the interesting part.** `.format()` requires every declared variable to be
supplied or it raises `TemplateFormatterError: Missing template variable(s): ...` — so the raw
(unsubstituted) text can't be requested directly. The workaround, verified before trusting it:

```python
# feed each variable its OWN NAME back as the substitution value
pv.format(variables={"name": "{name}", "score": "{score}"}, sdk="openai")
# -> "Hello {name}, your score is {score}"   — byte-identical to the original
```

Confirmed against a synthetic example first, then against all seven real prompt files — checked none
of them contain a stray brace outside a declared variable, which would have broken the round-trip.

### 3.2 ⚠️ The idempotency bug — the one a mock could not have caught

A freshly created prompt version is **untagged**. `_exists()` and `_fetch()` both query
`get(tag="production")`, and an untagged version never matches — so every startup found nothing,
concluded the prompt didn't exist, and created a new version. Forever.

```
RUN 1: created 7
RUN 2: created 7      ← should be 0. Every "push" was a new version.
```

Fixed by adding the missing second call:

```python
created = client.prompts.create(name=name, version=version)
client.prompts.tags.create(prompt_version_id=created.id, name=self._settings.prompt_tag)
```

```
RUN 1: created 7
RUN 2: created 0      ← fixed
```

**Why a mock wouldn't have caught this:** a mock verifies `create` was *called*. It can't tell you
whether the object that call produced is *findable afterwards* — that requires a real round trip
through the actual server.

### 3.3 The pytest marker name collision

`arize-phoenix-client` registers its own pytest plugin (`phoenix.client.pytest.plugin`), which reserves
the marker name **`phoenix`** for its own purpose — *"record this test as a Phoenix experiment run."*
The live-Phoenix tests here were marked `@pytest.mark.phoenix`, intending only "needs a running Phoenix
server."

Both meanings applied to the same tests simultaneously. The plugin's own experiment-tracking behaviour
redirected spans into ad-hoc `Experiment-...` projects instead of the configured `rag-serving` /
`rag-ingestion` projects — **intermittently**, only once other tests had run first and accumulated
enough suite-level state to trigger it. Tests passed reliably in isolation and failed unpredictably as
part of the full suite, which is what made this take longest to pin down.

```
plugins: cov-7.1.0, asyncio-1.4.0, anyio-4.14.2, arize-phoenix-client-2.13.0
```

Found by listing registered `pytest11` entry points and reading the plugin's own source — the
docstring on its `phoenix` marker states its meaning explicitly.

**Fix:** renamed the custom marker to `live_phoenix`, with the collision explained in a comment at the
point of definition so it can't be reintroduced by accident.

### 3.4 Two separate timing bugs, both fixed properly rather than papered over

**Client-side race.** `BatchSpanProcessor` exports on its own schedule, not the instant a span ends. A
fixed `time.sleep(2)` after creating a span is a race, not a wait — the very first manual check
"passed" only because enough wall-clock time happened to elapse between two separate tool calls.
Fixed with a deterministic `force_flush()`, exposed as `tracing.flush()`.

**Server-side read lag — a different bug, found only after the first was fixed.** Even with a
deterministic flush, one test still failed intermittently under full-suite load, passing every time in
isolation. `flush()` guarantees Phoenix's HTTP endpoint *received* the span; it says nothing about how
quickly Phoenix indexes it for the **query** API — a gap normally too small to notice, that widened
under concurrent load from other tests. Fixed with a bounded poll on the read side (`_wait_for_span`,
up to 5 s), which is the correct fix for read-after-write lag — more client-side flushing does not
touch it, since the SDK's part was already complete.

```bash
for i in 1 2 3 4 5; do uv run pytest -q 2>&1 | tail -1; done
```

```
25 passed   25 passed   25 passed   25 passed   25 passed
```

Five consecutive clean runs is what actually closes this out — a single pass proves nothing about an
intermittent failure.

---

## 4. Final state

```
PROMPTS      seven bundled, seven pushed to Phoenix, correctly tagged
             idempotent — a second push creates zero new versions
             round-trip text recovery verified byte-exact against all seven files

TRACING      spans confirmed queryable back from Phoenix's own API
             serving and ingestion projects verified isolated from each other

TESTS        25 passed — 21 unit/fallback, 4 live-Phoenix integration
             5 consecutive full-suite runs, zero flakes

QUALITY      ruff clean · ruff format clean · mypy strict clean
```

---

## 5. What this unblocks

Every later phase that makes a model call or uses a prompt can now satisfy two lines of the shared
Definition of Done — *traced in Phoenix* and *prompt in the registry, not code* — without any
coordination, because the infrastructure already exists and is proven working against the real
service, not just designed on paper.

---

## 6. Command reference

```bash
uv sync --extra observability                                   # install the extras

uv run pytest tests/unit/test_prompts.py -q                     # fallback path, no server needed
uv run pytest -m live_phoenix -v                                # against a running Phoenix
uv run pytest -q                                                 # everything

# manual push/verify, useful when iterating on a prompt file
uv run python -c "
from app.shared.config import get_settings
from app.shared.prompts import PromptRegistry
r = PromptRegistry(get_settings().phoenix)
print('created:', r.push_bundled())
"
```

| Surface | URL |
|---|---|
| Phoenix UI — prompts and traces | http://localhost:6006 |
