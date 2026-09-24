"""One-off helper: create the disposable PostgreSQL test database.

Reads DATABASE_URL from the environment/.env (never prints it), derives the
server address, and creates TEST_DB_NAME if it does not exist. Safe to re-run.
"""
from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlsplit

import asyncpg

TEST_DB_NAME = "agenthub_test"


def _admin_dsn() -> str:
    """Admin DSN (maintenance DB) for raw asyncpg: postgresql:// scheme."""
    from app.core.config import settings

    url = settings.DATABASE_URL
    if not url.startswith("postgresql+asyncpg://"):
        raise SystemExit("DATABASE_URL must start with postgresql+asyncpg://")
    parts = urlsplit(url)
    # asyncpg's raw client rejects the SQLAlchemy '+asyncpg' dialect suffix.
    netloc = parts.netloc
    admin = f"postgresql://{netloc}/postgres"
    return admin


async def main() -> int:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME
        )
        if exists:
            print(f"OK: database '{TEST_DB_NAME}' already exists")
            return 0
        await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
        print(f"Created database '{TEST_DB_NAME}'")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
