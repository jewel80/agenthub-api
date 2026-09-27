"""Reader/writer session split (scale-doc §1): reader is read-only,
read-your-writes, the replica-unhealthy fail-over, and "replica disabled
behaves exactly as before" (scale-doc §1.3).

The read-your-writes tests use real Redis the same way tests/test_cache_
service.py and tests/test_redis_limiter.py do (127.0.0.1:6380; skip if
unreachable) — see the 127.0.0.1-not-localhost note in test_redis_limiter.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import db as core_db
from app.core import redis as redis_mod
from app.core.config import settings
from tests.conftest import TEST_DB_URL

TEST_REDIS_URL = "redis://127.0.0.1:6380/0"


async def test_replica_disabled_by_default():
    """The default config for this milestone (scale-doc §10 step 2): ships
    with the replica off, reader == primary."""
    assert settings.DB_READ_REPLICA_ENABLED is False
    assert core_db._replica_configured is False


async def test_reader_session_rejects_an_insert():
    """A misrouted write through a reader session fails loudly (scale-doc
    §1.2.3), proven against the real test database — not mocked."""
    reader_engine = create_async_engine(
        TEST_DB_URL, connect_args=core_db.build_reader_connect_args()
    )
    try:
        async with reader_engine.connect() as conn:
            with pytest.raises(DBAPIError):
                # DDL is blocked in a read-only transaction just like DML —
                # avoids depending on any particular table's columns.
                await conn.execute(text("CREATE TEMP TABLE ro_probe (id int)"))
    finally:
        await reader_engine.dispose()


async def test_catalog_endpoint_reads_still_work_with_replica_disabled(client, seeded):
    """scale-doc §1.3: "with replica disabled, every endpoint behaves
    exactly as before" — the catalog now reads via get_read_db (see
    app/api/routers/agents.py) and must still work end to end."""
    r = await client.get("/agents")
    assert r.status_code == 200
    slugs = {a["slug"] for a in r.json()}
    assert {"doctor-physician", "corporate-lawyer"} <= slugs


def test_unhealthy_replica_routes_to_primary(monkeypatch):
    """scale-doc §1.3: a simulated unhealthy replica routes reads to the
    primary. Pure routing-logic test (sentinel session factories) — real DB
    I/O through the reader/writer split is already covered by the full
    suite via the conftest dependency overrides."""

    class _FakeSessionCM:
        def __init__(self, marker):
            self.marker = marker

        async def __aenter__(self):
            return self.marker

        async def __aexit__(self, *exc_info):
            return False

    sentinel_writer, sentinel_reader = object(), object()
    monkeypatch.setattr(
        core_db, "AsyncSessionLocal", lambda: _FakeSessionCM(sentinel_writer)
    )
    monkeypatch.setattr(
        core_db, "ReaderSessionLocal", lambda: _FakeSessionCM(sentinel_reader)
    )

    async def _drain(replica_configured: bool, replica_healthy: bool):
        monkeypatch.setattr(core_db, "_replica_configured", replica_configured)
        monkeypatch.setattr(core_db, "_replica_healthy", replica_healthy)
        async for session in core_db.get_read_db():
            return session

    import asyncio

    assert asyncio.run(_drain(True, True)) is sentinel_reader
    assert asyncio.run(_drain(True, False)) is sentinel_writer  # unhealthy -> primary
    assert (
        asyncio.run(_drain(False, False)) is sentinel_reader
    )  # no replica -> "reader" path (== primary)


@pytest.fixture
async def live_redis():
    """Point the shared client at the local test Redis; skip if unreachable."""
    saved_url = settings.REDIS_URL
    settings.REDIS_URL = TEST_REDIS_URL
    redis_mod._client = None
    redis_mod._unavailable = False
    try:
        ok = await redis_mod.ping()
        if not ok:
            pytest.skip("Redis not reachable at 127.0.0.1:6380")
        yield redis_mod
    finally:
        await redis_mod.close()
        settings.REDIS_URL = saved_url
        redis_mod._client = None
        redis_mod._unavailable = False


async def test_read_your_writes_marks_and_expires(live_redis, monkeypatch):
    """scale-doc §1.2.5: after mark_read_your_writes(), the window is active
    (reads should go to the writer); once READ_YOUR_WRITES_WINDOW_SECONDS
    elapses, it clears itself (Redis key TTL) and reads go back to the
    reader. Only meaningful when a replica is configured."""
    import asyncio

    monkeypatch.setattr(core_db, "_replica_configured", True)
    monkeypatch.setattr(settings, "READ_YOUR_WRITES_WINDOW_SECONDS", 1)
    user_id = uuid.uuid4()

    assert await core_db.read_your_writes_active(user_id) is False

    await core_db.mark_read_your_writes(user_id)
    assert await core_db.read_your_writes_active(user_id) is True

    await asyncio.sleep(1.2)
    assert await core_db.read_your_writes_active(user_id) is False


async def test_read_your_writes_moot_without_a_replica(live_redis, monkeypatch):
    """No replica configured -> the reader already *is* the primary, so
    read-your-writes never needs to kick in, even after marking a write."""
    monkeypatch.setattr(core_db, "_replica_configured", False)
    user_id = uuid.uuid4()
    await core_db.mark_read_your_writes(user_id)  # no-op: nothing to protect
    assert await core_db.read_your_writes_active(user_id) is False


async def test_read_your_writes_fails_safe_when_redis_down(monkeypatch):
    """scale-doc §1.2.5: "If Redis is down -> use the writer (safe)" — when
    a replica IS configured but we can't check the flag, fail toward
    consistency (True), not toward "assume no recent write"."""
    saved_url = settings.REDIS_URL
    settings.REDIS_URL = "redis://127.0.0.1:6399/0"  # nothing listens there
    redis_mod._client = None
    redis_mod._unavailable = False
    monkeypatch.setattr(core_db, "_replica_configured", True)
    try:
        assert await core_db.read_your_writes_active(uuid.uuid4()) is True
    finally:
        await redis_mod.close()
        settings.REDIS_URL = saved_url
        redis_mod._client = None
        redis_mod._unavailable = False
