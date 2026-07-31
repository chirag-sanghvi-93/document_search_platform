-- Runs once, on first initialisation of the Postgres volume.
--
-- Only the extension is created here. Tables belong to Alembic migrations (E3),
-- so that schema changes are versioned rather than applied by a script that runs
-- exactly once and is then unreachable.

CREATE EXTENSION IF NOT EXISTS vector;
