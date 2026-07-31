"""Engine and session factory.

One engine per process, reused across requests — creating a new engine per call
would defeat connection pooling. `get_session` is a context manager so callers
never forget to close what they opened.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.shared.config import Settings, get_settings


@lru_cache
def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(settings.db.url, pool_pre_ping=True)


@lru_cache
def _session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(settings), expire_on_commit=False)


@contextmanager
def get_session(settings: Settings | None = None) -> Iterator[Session]:
    """One session per unit of work. Commits on clean exit, rolls back on
    exception — callers never need to remember either half."""
    session = _session_factory(settings)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
