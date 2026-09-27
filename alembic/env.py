"""Alembic environment — async, driven by app settings.

Reads DATABASE_URL from app settings so the same env var used at runtime
drives migrations. The ALEMBIC_DATABASE_URL environment variable overrides
the target database (used by the test suite to migrate its throwaway DB).
Connect args (SSL / pgbouncer) mirror the application engine.
"""
from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import settings
from app.core.db import build_connect_args
from app.core.db_safety import db_name
from app.models import Base

config = context.config

url = os.environ.get("ALEMBIC_DATABASE_URL") or settings.DATABASE_URL
# Non-negotiable rule §2.10: always show which DB a migration command will
# touch (name only, never credentials) before it runs.
print(f"[alembic] target database: {db_name(url) or '(none)'}")
# configparser treats '%' as interpolation syntax (URL-encoded passwords
# contain %40 etc.) — escape per the Alembic cookbook.
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args=build_connect_args(),
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


run_migrations_online()
