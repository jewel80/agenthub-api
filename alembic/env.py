"""Alembic environment — async, driven by app settings.

Reads DATABASE_URL from app settings so the same env var used at runtime
drives migrations. Connect args (Supabase SSL / pgbouncer) are applied too.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings
from app.models import Base  # noqa: F401  (registers tables on metadata)

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _connect_args() -> dict:
    url = settings.DATABASE_URL
    if url.startswith("sqlite"):
        return {"check_same_thread": False}
    if url.startswith("postgres"):
        return {"statement_cache_size": 0, "ssl": "require"}
    return {}


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=url_is_sqlite(),
    )
    with context.begin_transaction():
        context.run_migrations()


def url_is_sqlite() -> bool:
    return settings.DATABASE_URL.startswith("sqlite")


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=_connect_args(),
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


run_migrations_online()
