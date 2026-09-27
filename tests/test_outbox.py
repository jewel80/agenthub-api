"""Transactional outbox (scale-doc §3): event written iff the transaction
commits, the relay publishes exactly once per event, dead-lettering after
too many failed attempts, cleanup, and consumer idempotency on a duplicate
delivery (scale-doc §3's own three required test scenarios, plus the DLQ
and cleanup DoD items).

Relay/consumer tests use real Redis the same way tests/test_cache_service.py
and tests/test_redis_limiter.py do (127.0.0.1:6380; skip if unreachable).
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core import redis as redis_mod
from app.core.config import settings
from app.models.outbox import OutboxEvent
from app.repositories import outbox_repo
from app.services import outbox_consumer, outbox_relay

TEST_REDIS_URL = "redis://127.0.0.1:6380/0"


@pytest.fixture(autouse=True)
def _outbox_enabled(monkeypatch):
    monkeypatch.setattr(settings, "OUTBOX_ENABLED", True)


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


async def test_event_written_iff_transaction_commits(session_factory):
    async with session_factory() as db:
        event = await outbox_repo.add_event(
            db, event_type="test.rollback", payload={"k": "v"}
        )
        assert event is not None
        await db.rollback()

    async with session_factory() as db:
        res = await db.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "test.rollback")
        )
        assert res.scalars().first() is None  # rolled back -> never written

    async with session_factory() as db:
        await outbox_repo.add_event(db, event_type="test.commit", payload={"k": "v"})
        await db.commit()

    async with session_factory() as db:
        res = await db.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "test.commit")
        )
        assert res.scalars().first() is not None  # committed -> durably written


async def test_add_event_is_a_noop_when_disabled(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "OUTBOX_ENABLED", False)
    async with session_factory() as db:
        event = await outbox_repo.add_event(
            db, event_type="test.disabled", payload={}
        )
        assert event is None
        await db.commit()

    async with session_factory() as db:
        res = await db.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "test.disabled")
        )
        assert res.scalars().first() is None


async def test_claim_batch_locks_unpublished_oldest_first(session_factory):
    async with session_factory() as db:
        await outbox_repo.add_event(db, event_type="test.order", payload={"n": 1})
        await outbox_repo.add_event(db, event_type="test.order", payload={"n": 2})
        await db.commit()

    async with session_factory() as db:
        batch = await outbox_repo.claim_batch(db, batch_size=10)
        ours = [e for e in batch if e.event_type == "test.order"]
        assert [e.payload["n"] for e in ours] == [1, 2]
        await db.rollback()  # release the row locks without publishing


async def test_relay_publishes_exactly_once_per_event(session_factory, live_redis):
    event_type = f"test.relay.{uuid.uuid4().hex[:8]}"
    await redis_mod.call("delete", outbox_relay.stream_name(event_type))

    async with session_factory() as db:
        await outbox_repo.add_event(db, event_type=event_type, payload={"x": 1})
        await db.commit()

    processed = await outbox_relay.relay_once(session_factory)
    assert processed >= 1

    length = await redis_mod.call("xlen", outbox_relay.stream_name(event_type))
    assert length == 1  # published exactly once

    async with session_factory() as db:
        res = await db.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == event_type)
        )
        row = res.scalars().first()
        assert row.published_at is not None

    # A second relay pass must not re-publish an already-published row.
    await outbox_relay.relay_once(session_factory)
    length_again = await redis_mod.call("xlen", outbox_relay.stream_name(event_type))
    assert length_again == 1


async def test_relay_moves_to_dlq_after_max_attempts(session_factory, live_redis):
    event_type = f"test.dlq.{uuid.uuid4().hex[:8]}"
    await redis_mod.call("delete", outbox_relay.dlq_stream_name(event_type))

    async with session_factory() as db:
        event = await outbox_repo.add_event(
            db, event_type=event_type, payload={"x": 1}
        )
        event.attempts = settings.OUTBOX_MAX_ATTEMPTS  # already exhausted retries
        await db.commit()

    await outbox_relay.relay_once(session_factory)

    dlq_length = await redis_mod.call("xlen", outbox_relay.dlq_stream_name(event_type))
    assert dlq_length == 1

    async with session_factory() as db:
        res = await db.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == event_type)
        )
        row = res.scalars().first()
        assert row.published_at is not None  # handled (DLQ'd), not retried forever


async def test_relay_records_failure_when_redis_down(session_factory, monkeypatch):
    saved_url = settings.REDIS_URL
    settings.REDIS_URL = "redis://127.0.0.1:6399/0"  # nothing listens there
    redis_mod._client = None
    redis_mod._unavailable = False
    try:
        async with session_factory() as db:
            await outbox_repo.add_event(
                db, event_type="test.redis-down", payload={}
            )
            await db.commit()

        await outbox_relay.relay_once(session_factory)

        async with session_factory() as db:
            res = await db.execute(
                select(OutboxEvent).where(OutboxEvent.event_type == "test.redis-down")
            )
            row = res.scalars().first()
            assert row.published_at is None  # not lost — still pending, retryable
            assert row.attempts == 1
            assert row.last_error is not None
    finally:
        await redis_mod.close()
        settings.REDIS_URL = saved_url
        redis_mod._client = None
        redis_mod._unavailable = False


async def test_cleanup_deletes_only_old_published_events(session_factory):
    import datetime as dt

    async with session_factory() as db:
        old = await outbox_repo.add_event(
            db, event_type="test.cleanup.old", payload={}
        )
        old.published_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        recent = await outbox_repo.add_event(
            db, event_type="test.cleanup.recent", payload={}
        )
        recent.published_at = dt.datetime.now(dt.UTC)
        await outbox_repo.add_event(
            db, event_type="test.cleanup.unpublished", payload={}
        )
        await db.commit()

    async with session_factory() as db:
        deleted = await outbox_repo.cleanup_published(db, older_than_days=7)
        await db.commit()
    assert deleted == 1

    async with session_factory() as db:
        res = await db.execute(select(OutboxEvent.event_type))
        remaining = {row[0] for row in res.all()}
    assert "test.cleanup.old" not in remaining
    assert "test.cleanup.recent" in remaining
    assert "test.cleanup.unpublished" in remaining


async def test_consumer_is_idempotent_on_a_duplicate_delivery(live_redis):
    event_type = f"test.consume.{uuid.uuid4().hex[:8]}"
    stream = outbox_relay.stream_name(event_type)
    await redis_mod.call("delete", stream)

    calls: list[dict] = []

    async def handler(event_id: str, payload: dict) -> None:
        calls.append(payload)

    # A fresh domain event id every run: real Redis (unlike the ephemeral
    # fakeredis used elsewhere) persists the dedupe key across separate
    # pytest invocations within its TTL — a fixed id would false-negative
    # against a prior run's leftover key (see docs/PROGRESS.md M3 §3 notes).
    domain_event_id = f"dup-{uuid.uuid4().hex[:12]}"
    await redis_mod.call(
        "xadd", stream, {"id": domain_event_id, "payload": '{"n": 1}'}
    )

    n1 = await outbox_consumer.consume_once(event_type, handler)
    assert n1 == 1
    assert len(calls) == 1

    # Simulate redelivery of the same domain event id (e.g. after a crash
    # before ack) by re-adding it and consuming again: the handler must not
    # run twice for the same event id, even though the consumer sees a new
    # stream entry (a distinct redelivery, same underlying event).
    await redis_mod.call("xadd", stream, {"id": domain_event_id, "payload": '{"n": 1}'})
    n2 = await outbox_consumer.consume_once(event_type, handler)
    assert n2 == 1  # message was read...
    assert len(calls) == 1  # ...but the handler did not run again


async def test_consumer_leaves_failed_handler_unacked_for_redelivery(live_redis):
    event_type = f"test.consume-fail.{uuid.uuid4().hex[:8]}"
    stream = outbox_relay.stream_name(event_type)
    await redis_mod.call("delete", stream)

    attempts = 0

    async def flaky_handler(event_id: str, payload: dict) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated transient failure")

    domain_event_id = f"flaky-{uuid.uuid4().hex[:8]}"
    # xack needs the real stream entry id, not our own "id" field's value —
    # capture what xadd actually returns rather than the domain id.
    stream_entry_id = await redis_mod.call(
        "xadd", stream, {"id": domain_event_id, "payload": "{}"}
    )

    await outbox_consumer.consume_once(event_type, flaky_handler)
    assert attempts == 1  # failed -> left unacked

    # Not yet acked by the failed attempt (ack only happens on success) —
    # our own manual ack of the real stream entry id should still succeed,
    # proving the failed delivery was never acked (and so remains pending
    # for the group's own redelivery mechanism).
    ok = await redis_mod.call("xack", stream, outbox_consumer._GROUP, stream_entry_id)
    assert ok == 1
