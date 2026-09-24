"""Async database engine + session factory — PostgreSQL only.

Connection behaviour (SSL, pool sizing, PgBouncer compatibility) is driven by
app settings; there is no SQLite path.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def build_connect_args() -> dict:
    """asyncpg connect args derived from settings.

    - SSL is required unless DB_SSL_MODE=disable (typical for local dev).
    - Behind PgBouncer (transaction mode) prepared statements must not be
      cached, so the asyncpg statement cache is disabled.
    """
    args: dict = {}
    if settings.DB_SSL_MODE == "require":
        args["ssl"] = "require"
    if settings.DB_USE_PGBOUNCER:
        args["statement_cache_size"] = 0
    return args


def build_engine_kwargs() -> dict:
    kwargs: dict = {
        "future": True,
        "echo": False,
        "pool_pre_ping": True,
        "connect_args": build_connect_args(),
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
    }
    if settings.DB_USE_PGBOUNCER:
        # SQLAlchemy-side prepared-statement cache must also be off behind a
        # transaction-mode pooler.
        kwargs["prepared_statement_cache_size"] = 0
    return kwargs


engine = create_async_engine(settings.DATABASE_URL, **build_engine_kwargs())

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a scoped async DB session."""
    async with AsyncSessionLocal() as session:
        yield session
