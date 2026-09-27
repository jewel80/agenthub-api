"""Safe migration-cycle verification: `alembic downgrade base` -> `upgrade head`
against a disposable database only.

This replaces running that cycle by hand against whatever `DATABASE_URL`
happens to resolve to — the exact mistake that wiped the local dev DB during
the M0 audit (see docs/PROGRESS.md and docs/CLAUDE_CODE_INSTRUCTIONS.md §2).
It always targets `TEST_DATABASE_URL` (or `--url`), prints the target
database name first, and refuses outright if that name matches the main
`DATABASE_URL` database.

Usage:
    python -m scripts.verify_migration_cycle
    python -m scripts.verify_migration_cycle --url postgresql+asyncpg://.../throwaway_db
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic.config import Config

from alembic import command
from app.core.config import settings
from app.core.db_safety import MainDatabaseGuardError, refuse_if_main_db

_REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=settings.TEST_DATABASE_URL,
        help="Target DB (default: TEST_DATABASE_URL). Never DATABASE_URL.",
    )
    args = parser.parse_args(argv)

    if not args.url:
        print(
            "ERROR: no target URL. Set TEST_DATABASE_URL or pass --url.",
            file=sys.stderr,
        )
        return 2
    if not args.url.startswith("postgresql+asyncpg://"):
        print("ERROR: target must be postgresql+asyncpg://...", file=sys.stderr)
        return 2

    try:
        refuse_if_main_db(
            args.url, settings.DATABASE_URL, label="verify_migration_cycle"
        )
    except MainDatabaseGuardError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3

    os.environ["ALEMBIC_DATABASE_URL"] = args.url
    alembic_cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))

    print("Running: alembic downgrade base")
    command.downgrade(alembic_cfg, "base")
    print("Running: alembic upgrade head")
    command.upgrade(alembic_cfg, "head")
    print("OK: migration cycle clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
