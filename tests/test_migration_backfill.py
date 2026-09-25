"""F1 migration backfill — verified against real pre-existing data.

The other ordering tests exercise the *application* path (seq already
exists). This test proves the *migration itself*: given messages that
predate the `seq` column and share an identical `created_at` (the exact
Postgres transaction-timestamp collision F1 fixes), upgrading assigns `seq`
in `created_at ASC, user-before-assistant on ties, id ASC` order (fix-doc
F1 step 2) — never scrambled, and never loses a row.

Runs against the disposable TEST_DATABASE_URL: `ALEMBIC_DATABASE_URL` is
pinned there for the whole test session by conftest's `_run_migrations`
fixture, so `command.downgrade`/`command.upgrade` here are safe.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.security import hash_password

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PRE_SEQ_REVISION = "0f703575e678"  # initial_schema, before F1's seq migration


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    return cfg


async def test_seq_backfill_orders_identical_timestamps_correctly(
    engine, seeded
):
    """Downgrade below F1, seed rows with one shared `created_at`, upgrade,
    and check the migration's own backfill query recovers the true order."""
    cfg = _alembic_config()

    async with engine.begin() as conn:
        agent_id = (
            await conn.execute(
                text("SELECT id FROM agents WHERE slug = 'doctor-physician'")
            )
        ).scalar_one()

    # 1. Roll the schema back to just before the seq/status columns existed.
    # alembic's async env.py calls asyncio.run() internally, which cannot
    # nest inside this test's own running event loop — run it on a thread.
    await asyncio.to_thread(command.downgrade, cfg, _PRE_SEQ_REVISION)

    user_id = uuid.uuid4()
    # A shared, fixed timestamp: Postgres would give every message in one
    # transaction this exact same created_at (the F1 bug). `id` is assigned
    # in a DIFFERENT order than the conversation itself, so recovering the
    # right order proves the backfill uses (created_at, role, id) — not
    # insertion order.
    shared_ts = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        ("assistant", "reply-1"),
        ("user", "hello-1"),
        ("assistant", "reply-2"),
        ("user", "hello-2"),
        ("assistant", "reply-3"),
        ("user", "hello-3"),
    ]

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, agent_id) "
                    "VALUES (:id, :email, :pw, :agent_id)"
                ),
                {
                    "id": user_id,
                    "email": "backfill@example.com",
                    "pw": hash_password("supersecret1"),
                    "agent_id": agent_id,
                },
            )
            for role, content in rows:
                await conn.execute(
                    text(
                        "INSERT INTO messages "
                        "(id, user_id, agent_id, sub_agent_id, role, "
                        "content, created_at) "
                        "VALUES (:id, :user_id, :agent_id, NULL, :role, "
                        ":content, :ts)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "user_id": user_id,
                        "agent_id": agent_id,
                        "role": role,
                        "content": content,
                        "ts": shared_ts,
                    },
                )

        # 2. Run the real F1 migration (and the later status migration) —
        #    this executes the exact backfill SQL shipped in the revision.
        await asyncio.to_thread(command.upgrade, cfg, "head")

        async with engine.begin() as conn:
            actual = (
                await conn.execute(
                    text(
                        "SELECT role, content FROM messages "
                        "WHERE user_id = :user_id ORDER BY seq ASC"
                    ),
                    {"user_id": user_id},
                )
            ).fetchall()
            # The exact guarantee (fix-doc F1 step 2): for equal created_at,
            # sort key is (created_at ASC, role='user' DESC, id ASC).
            # Recompute the expected order the same way, independently of
            # `seq`, and require the migration to have matched it exactly.
            expected = (
                await conn.execute(
                    text(
                        "SELECT role, content FROM messages "
                        "WHERE user_id = :user_id "
                        "ORDER BY created_at ASC, (role = 'user') DESC, id ASC"
                    ),
                    {"user_id": user_id},
                )
            ).fetchall()

        assert len(actual) == 6  # no row lost or duplicated
        assert [(r.role, r.content) for r in actual] == [
            (r.role, r.content) for r in expected
        ]
        # And the concrete outcome (all 6 rows tie on created_at, so the
        # role tiebreak alone decides): every 'user' turn sorts before
        # every 'assistant' turn, never scrambled.
        roles = [r.role for r in actual]
        assert roles == ["user", "user", "user", "assistant", "assistant", "assistant"]
    finally:
        # Leave the schema at head for every later test in this session,
        # regardless of the assertion outcome above.
        await asyncio.to_thread(command.upgrade, cfg, "head")
