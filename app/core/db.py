"""Async database engine + session factory — PostgreSQL only.

Connection behaviour (SSL, pool sizing, PgBouncer compatibility) is driven by
app settings; there is no SQLite path.

Reader/writer split (scale-doc §1): `get_db` is the writer (read-write,
targets DATABASE_URL — this *is* the doc's "get_write_db()", kept under its
existing name to avoid touching every write-path call site for a rename).
`get_read_db` is the reader: a read-only session (misrouted writes fail
loudly) targeting the replica when `DB_READ_REPLICA_ENABLED` + a healthy
`DATABASE_READ_URL`, otherwise the primary — either way the code path is
identical, only the target changes. With the replica disabled (the default
here — scale-doc §10 step 2 ships it disabled), the reader is a read-only
session against the primary: same data, same behavior, extra safety.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core import redis as redis_client
from app.core.config import settings

logger = logging.getLogger("agenthub.db")


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
    """FastAPI dependency that yields a scoped async (writer) DB session."""
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Reader/writer split (scale-doc §1)
# ---------------------------------------------------------------------------

# Computed once at import (env-driven, not expected to change at runtime).
# Tests that need to simulate "replica configured" monkeypatch this flag
# directly rather than reloading settings/re-importing the module.
_replica_configured = bool(
    settings.DB_READ_REPLICA_ENABLED and settings.DATABASE_READ_URL
)
_reader_target_url = (
    settings.DATABASE_READ_URL if _replica_configured else settings.DATABASE_URL
)

# Lag-guard state (scale-doc §1.2.6); only meaningful when a replica is
# actually configured. Healthy by default so a not-yet-checked replica
# doesn't get treated as down before the first guard tick.
_replica_healthy = True
_replica_lag_seconds = 0.0


def build_reader_connect_args() -> dict:
    """Reader connect args: same as the writer, plus a Postgres-side
    read-only session (scale-doc §1.2.3) — a misrouted write fails loudly
    with a DB error instead of silently succeeding against the wrong node.
    """
    args = build_connect_args()
    server_settings = dict(args.get("server_settings") or {})
    server_settings["default_transaction_read_only"] = "on"
    args["server_settings"] = server_settings
    return args


def build_reader_engine_kwargs() -> dict:
    kwargs = build_engine_kwargs()
    kwargs["connect_args"] = build_reader_connect_args()
    return kwargs


reader_engine = create_async_engine(_reader_target_url, **build_reader_engine_kwargs())

ReaderSessionLocal = async_sessionmaker(
    reader_engine, class_=AsyncSession, expire_on_commit=False
)


async def get_read_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: a read-only DB session (scale-doc §1).

    Targets the replica when configured and currently healthy; otherwise
    the primary. Read-only either way, so a route that accidentally writes
    through this dependency fails immediately rather than silently
    succeeding against the wrong node. For a route whose read must reflect
    the caller's own very-recent write (read-your-writes), use
    `app.core.deps.get_read_db_for_user` instead.
    """
    if _replica_configured and not _replica_healthy:
        # Replica configured but currently unhealthy: fail over to the
        # primary rather than serve (possibly very) stale data.
        async with AsyncSessionLocal() as session:
            yield session
        return
    async with ReaderSessionLocal() as session:
        yield session


async def read_your_writes_active(user_id: uuid.UUID | str) -> bool:
    """True when `user_id` wrote recently enough that reads should still go
    to the writer (scale-doc §1.2.5). Always False when no replica is
    configured — the reader already *is* the primary, so there's nothing to
    protect against. When a replica is configured, "can't tell" (Redis
    unavailable/unconfigured) fails safe toward the writer.
    """
    if not _replica_configured:
        return False
    if not settings.REDIS_URL:
        return True  # replica exists but nothing to check the flag against
    key = f"agenthub:{settings.ENVIRONMENT}:ryw:{user_id}"
    exists = await redis_client.call("get", key)
    if exists is not None:
        return True
    # `call()` returns None for both "confirmed absent" and "Redis errored" —
    # check is_unavailable() *after* the attempt (it reflects whether this
    # very call just failed) to tell those two apart and fail safe.
    return redis_client.is_unavailable()


async def mark_read_your_writes(user_id: uuid.UUID | str) -> None:
    """Call after a write by `user_id` (scale-doc §1.2.5). A no-op — safely,
    via the shared Redis client's own degradation — when no replica is
    configured or Redis is unavailable/unconfigured.
    """
    if not _replica_configured:
        return
    key = f"agenthub:{settings.ENVIRONMENT}:ryw:{user_id}"
    await redis_client.call(
        "set", key, "1", ex=settings.READ_YOUR_WRITES_WINDOW_SECONDS
    )


async def check_replica_lag() -> None:
    """One lag check against the replica (scale-doc §1.2.6): unreachable or
    over `DB_REPLICA_MAX_LAG_SECONDS` marks it unhealthy so `get_read_db`
    fails over to the primary until it recovers.
    """
    global _replica_healthy, _replica_lag_seconds
    try:
        async with reader_engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM "
                    "(now() - pg_last_xact_replay_timestamp()))"
                )
            )
            lag = result.scalar()
        _replica_lag_seconds = float(lag) if lag is not None else 0.0
        _replica_healthy = _replica_lag_seconds <= settings.DB_REPLICA_MAX_LAG_SECONDS
    except Exception:
        logger.warning("replica lag check failed; marking unhealthy", exc_info=True)
        _replica_healthy = False


async def run_replica_lag_guard(interval_seconds: float = 5.0) -> None:
    """Background task: checks replica lag every `interval_seconds`
    (scale-doc §1.2.6). A no-op loop (returns immediately) when no replica
    is configured — nothing to guard.
    """
    if not _replica_configured:
        return
    while True:
        await check_replica_lag()
        await asyncio.sleep(interval_seconds)
