"""Guard against destructive commands hitting the main database by accident.

Born from an incident (see docs/PROGRESS.md, M0 audit): a manual
`alembic downgrade base && alembic upgrade head` verification step was run
without `ALEMBIC_DATABASE_URL` set, so it silently targeted `DATABASE_URL`
(the local dev DB) and wiped its data. Tests and migration-check scripts must
never repeat that: they refuse to run whenever their target database name
matches the main `DATABASE_URL`'s database name (docs/CLAUDE_CODE_INSTRUCTIONS.md §2).
"""
from __future__ import annotations

from urllib.parse import urlsplit


class MainDatabaseGuardError(RuntimeError):
    """Raised when a test/migration-check target resolves to the main DB."""


def db_name(url: str) -> str:
    """The database name (path component) of a DSN — never the credentials."""
    return urlsplit(url).path.lstrip("/")


def refuse_if_main_db(target_url: str, main_url: str, *, label: str) -> None:
    """Refuse (raise) if `target_url` names the same database as `main_url`.

    Always prints the target database name first (never credentials), per
    the non-negotiable rule this guard exists to enforce.
    """
    target, main = db_name(target_url), db_name(main_url)
    print(f"[{label}] target database: {target or '(none)'}")
    if target and main and target == main:
        raise MainDatabaseGuardError(
            f"{label}: refusing to run — target database '{target}' is the "
            "main DATABASE_URL database. Point this at TEST_DATABASE_URL or "
            "a throwaway database instead."
        )
