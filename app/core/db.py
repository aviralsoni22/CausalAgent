"""Database engine management.

A SQLAlchemy ``Engine`` owns a connection pool and is expensive to build, so it
must be created once per process and shared — never per request/task. This
module is the single place that constructs it, with pool settings tuned for
long-lived Celery workers that sit idle between tasks.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.core import config


def _build_engine(url: str) -> Engine:
    return create_engine(
        url,
        # Validate connections before use so a worker that has been idle (or a
        # DB restart / network blip) doesn't hand out a dead connection.
        pool_pre_ping=True,
        pool_size=config.DB_POOL_SIZE,
        max_overflow=config.DB_MAX_OVERFLOW,
        # Recycle connections periodically to dodge server-side idle timeouts.
        pool_recycle=config.DB_POOL_RECYCLE,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Process-wide pooled engine for the TRUSTED path (audit writes, ingest)."""
    return _build_engine(config.database_url())


@lru_cache(maxsize=1)
def get_readonly_engine() -> Engine:
    """Pooled engine for the UNTRUSTED query path (the SQL agent's extraction).

    Connects as a least-privilege role with SELECT on only the analytics tables,
    so an LLM-generated (and possibly prompt-injected) query is structurally
    unable to read other tables, write, or call superuser functions — regardless
    of what slips past the SQL agent's own validation.
    """
    return _build_engine(config.readonly_database_url())
