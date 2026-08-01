.PHONY: install lint format typecheck test test-unit up down logs ps models migrate seed ingest clean

# ---------------------------------------------------------------- development

install:                       ## Resolve and install everything, including dev tools
	uv sync --all-extras --group dev

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

# Tests are type-checked too, deliberately. A test that passed the wrong type
# (the top-level Settings where RetrievalSettings was expected) reached the
# graceful-degradation path instead of erroring, so it failed on an unrelated
# assertion and hid its own cause. Checking only `app` would miss that class of
# bug entirely.
typecheck:
	uv run mypy app tests

test:                          ## Everything, including tests needing Postgres and Ollama
	uv run pytest

test-unit:                     ## Fast subset — no services required
	uv run pytest -m "not integration and not models"

# ---------------------------------------------------------------------- stack

up:
	docker compose up -d --build
	@echo "Waiting for the backend to become ready..."
	@until curl -sf http://localhost:8000/health/ready >/dev/null 2>&1; do sleep 2; done
	@echo "Ready.  UI http://localhost:3000   admin http://localhost:8000/admin   traces http://localhost:6006"

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

# --------------------------------------------------------------------- models

# Ollama runs NATIVELY, not in Compose — containers on macOS cannot reach the
# Metal GPU, and a CPU-only model host is unusable for per-chunk ingestion work.
#
# Ordered by when each epic first needs it, so they can be pulled one at a time
# rather than as one 8.7 GB download.
models:                        ## Pull all three models into the native Ollama
	ollama pull bge-m3         # 1.2 GB — embedding model, used by fixtures and retrieval
	ollama pull qwen3:4b       # 2.5 GB — summarisation and verification
	ollama pull qwen2.5:3b     # 1.9 GB — contextualisation ONLY, must be non-reasoning
	ollama pull qwen2.5:7b     # 4.7 GB — planner, retrieval agent, synthesizer

# --------------------------------------------------------------------- schema

migrate:                       ## Apply migrations up to head — idempotent, safe to re-run
	uv run alembic upgrade head

# ------------------------------------------------------------------------ data

seed:                          ## Load the deterministic fixture corpus
	uv run python -m app.shared.store.seed

ingest:                        ## Ingest data/raw into COLLECTION
	uv run python -m app.engine.ingest.cli --collection $(or $(COLLECTION),default)

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
