"""Async database engine + session factory.

Works on both SQLite (local dev/tests, `sqlite+aiosqlite`) and Postgres
(Supabase, `postgresql+asyncpg`). Supabase connection tuning is applied
automatically when a Postgres URL is detected.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings


def _connect_args() -> dict:
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        # SQLite (async) needs this off for cross-connection use.
        return {"check_same_thread": False}
    if url.startswith("postgres"):
        # Supabase pgbouncer pooler cannot cache prepared statements;
        # the managed instance requires SSL.
        return {"statement_cache_size": 0, "ssl": "require"}
    return {}


engine = create_async_engine(
    settings.DATABASE_URL,
    future=True,
    echo=False,
    connect_args=_connect_args(),
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def get_db() -> AsyncSession:
    """FastAPI dependency that yields a scoped async DB session."""
    async with AsyncSessionLocal() as session:
        yield session
