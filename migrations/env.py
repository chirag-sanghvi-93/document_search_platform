from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.shared.config import get_settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Connection string comes from our own settings, not alembic.ini — one source of
# truth for how to reach Postgres, not two that can silently drift apart.
#
# `db.url` (not `db.sync_url`) — it specifies the `+psycopg` driver explicitly.
# A bare `postgresql://` defaults to psycopg2, which this project never installs;
# only psycopg3 is a dependency, and create_engine() would fail trying to import
# a driver that isn't there.
config.set_main_option("sqlalchemy.url", get_settings().db.url)

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# No ORM models — migrations are written by hand against the schema documented
# in doc/components/02b-pgvector-postgresql.md. Autogenerate has nothing to diff
# against, which is deliberate: it would need ORM models mirroring the schema a
# second time, and the two would drift.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
