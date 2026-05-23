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


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Return the process-wide pooled engine."""
    return create_engine(
        config.database_url(),
        # Validate connections before use so a worker that has been idle (or a
        # DB restart / network blip) doesn't hand out a dead connection.
        pool_pre_ping=True,
        pool_size=config.DB_POOL_SIZE,
        max_overflow=config.DB_MAX_OVERFLOW,
        # Recycle connections periodically to dodge server-side idle timeouts.
        pool_recycle=config.DB_POOL_RECYCLE,
    )
