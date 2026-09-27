"""Delete published outbox_events rows older than the retention window
(scale-doc §3 point 6). Safe to run repeatedly (idempotent — nothing to
delete once already cleaned). Intended to be invoked periodically (cron/
scheduled task); no scheduler is wired up in this project yet.

Usage:
    python -m scripts.cleanup_outbox
    python -m scripts.cleanup_outbox --older-than-days 14
"""
from __future__ import annotations

import argparse
import asyncio

from app.core.db import AsyncSessionLocal
from app.repositories import outbox_repo


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=None,
        help="Override OUTBOX_RETENTION_DAYS for this run.",
    )
    args = parser.parse_args(argv)

    async with AsyncSessionLocal() as db:
        deleted = await outbox_repo.cleanup_published(
            db, older_than_days=args.older_than_days
        )
        await db.commit()

    print(f"Deleted {deleted} published outbox event(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
