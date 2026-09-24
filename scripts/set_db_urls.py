"""One-off helper: set DATABASE_URL / TEST_DATABASE_URL in .env safely.

Prompts for the Postgres username and password (masked — nothing is echoed,
printed, or logged), verifies them with a real connection, URL-encodes them,
and rewrites both URL lines in .env. Idempotent and safe to re-run.

Run it in your own terminal from the repo root:
    .venv\\Scripts\\python scripts\\set_db_urls.py
"""
from __future__ import annotations

import asyncio
import getpass
from pathlib import Path
from urllib.parse import quote

import asyncpg

REPO = Path(__file__).resolve().parents[1]
ENV = REPO / ".env"
HOST = "localhost"
PORT = 5432
MAIN_DB = "AgentHub_DB"
TEST_DB = "agenthub_test"


async def _verify(user: str, password: str) -> bool:
    dsn = f"postgresql://{quote(user)}:{quote(password)}@{HOST}:{PORT}/postgres"
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # report type only — never the dsn/credentials
        print(f"FAILED: {type(exc).__name__} — credentials or server not reachable")
        return False
    await conn.close()
    return True


def _rewrite_env(user_enc: str, pass_enc: str) -> None:
    main_url = f"postgresql+asyncpg://{user_enc}:{pass_enc}@{HOST}:{PORT}/{MAIN_DB}"
    test_url = f"postgresql+asyncpg://{user_enc}:{pass_enc}@{HOST}:{PORT}/{TEST_DB}"
    lines = ENV.read_text(encoding="utf-8-sig").splitlines()
    out, seen_main, seen_test = [], False, False
    for line in lines:
        if line.startswith("DATABASE_URL="):
            out.append(f"DATABASE_URL={main_url}")
            seen_main = True
        elif line.startswith("TEST_DATABASE_URL="):
            out.append(f"TEST_DATABASE_URL={test_url}")
            seen_test = True
        else:
            out.append(line)
    if not seen_main:
        out.insert(0, f"DATABASE_URL={main_url}")
    if not seen_test:
        out.append(f"TEST_DATABASE_URL={test_url}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    user = input("Postgres username [postgres]: ").strip() or "postgres"
    password = getpass.getpass("Postgres password (input hidden): ")
    if not password:
        print("ABORTED: empty password")
        return 2
    if not asyncio.run(_verify(user, password)):
        return 2
    _rewrite_env(quote(user, safe=""), quote(password, safe=""))
    print("OK: credentials verified and .env updated (both URLs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
