# One image, run two ways: `uvicorn` for the backend, `celery` for the worker.
# Two processes with one dependency set and one build — see doc/02-architecture.md §6.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# ⚠️ The last four are OpenCV's runtime libraries, pulled in by Docling.
#
# Without them ingestion dies inside the container on `import cv2` with
# `ImportError: libxcb.so.1: cannot open shared object file`. It went unnoticed
# for a long time because ingestion was always run on the HOST via `uv run`,
# where the system already has them — the container had never actually parsed a
# PDF. A python-slim base carries no graphics stack at all.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
       libgl1 libglib2.0-0 libxcb1 libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so a source change does not invalidate the resolved layer.
COPY pyproject.toml uv.lock* README.md ./

# Base dependencies only. The heavy optional groups — docling, torch via
# sentence-transformers, crewai, ragas — are installed selectively via EXTRAS
# below, one dependency group per capability the deployment actually needs.
#
# Three reasons, and the third is the real one:
#   · a base image is ~200 MB rather than ~3 GB
#   · a plain dependency-only rebuild takes seconds, not minutes
#   · a dependency problem surfaces against the group that introduced it, rather
#     than appearing on day one as an undifferentiated wall of resolution errors
#
# Build with the extras this deployment needs, e.g.:
#   docker compose build --build-arg EXTRAS="--extra ingest" backend
ARG EXTRAS=""
RUN uv sync --no-install-project ${EXTRAS}

COPY app ./app
COPY prompts ./prompts
COPY migrations ./migrations
# ⚠️ alembic.ini too. Without it `alembic upgrade head` inside the container
# fails with "No 'script_location' key found in configuration" — the migrations
# are present but nothing tells Alembic where they are. It went unnoticed for a
# long time because migrations were always run on the HOST via `uv run`, where
# the file is simply there; the container had never been asked to migrate.
COPY alembic.ini ./
RUN uv sync ${EXTRAS}

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
