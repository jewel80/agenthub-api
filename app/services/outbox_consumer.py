"""Outbox event consumers (scale-doc §3 point 4).

Reads each event type's Redis Stream via a consumer group, dedupes on
event id (Streams give at-least-once delivery, not exactly-once — this is
the idempotency layer the doc calls for), calls the registered handler, and
acks. Runs as an in-process background task per handler (wired into
app/main.py's lifespan) — see outbox_relay.py's module docstring re: a
dedicated worker fleet being the natural next step, not this session's scope.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from app.core import redis as redis_client
from app.core.config import settings
from app.services.outbox_relay import stream_name

logger = logging.getLogger("agenthub.outbox.consumer")

Handler = Callable[[str, dict], Awaitable[None]]

_GROUP = "consumers"
# Long enough to outlast any realistic re-delivery window for a duplicate.
_DEDUPE_TTL_SECONDS = 3600
# Must stay well under the shared client's command socket timeout
# (app/core/redis.py::_REDIS_COMMAND_TIMEOUT_SECONDS, 200ms) — a BLOCK
# duration the client can't wait out just looks like a connection timeout
# to it, which used to trip the global circuit breaker on every empty poll
# (see docs/PROGRESS.md M3 §3 "Corrections"). Pacing between empty polls is
# `_IDLE_SLEEP_SECONDS` instead, not a long server-side BLOCK.
_BLOCK_MS = 50
_IDLE_SLEEP_SECONDS = 0.5
_BATCH = 50

# Streams whose group we've already ensured exists this process — skips a
# redundant XGROUP CREATE (and its expected BUSYGROUP response) on every
# single poll once the group is confirmed to exist.
_groups_ensured: set[str] = set()


def _dedupe_key(event_id: str) -> str:
    return f"agenthub:{settings.ENVIRONMENT}:outbox:seen:{event_id}"


async def _already_processed(event_id: str) -> bool:
    """Check-and-set in one round trip: `SET NX` returns None when the key
    was already present, i.e. this event id was already handled."""
    got = await redis_client.call(
        "set", _dedupe_key(event_id), "1", nx=True, ex=_DEDUPE_TTL_SECONDS
    )
    return got is None


async def consume_once(event_type: str, handler: Handler) -> int:
    """One consume pass for `event_type`'s stream. Ensures the consumer
    group exists, reads a batch, handles each (skipping but still acking
    duplicates), and acks on success. A handler exception leaves that
    message unacked for later redelivery via the group's pending list —
    it is never silently dropped. Returns the number of messages read.
    """
    stream = stream_name(event_type)
    consumer = f"{event_type}-consumer"
    if stream not in _groups_ensured:
        await redis_client.call("xgroup_create", stream, _GROUP, id="0", mkstream=True)
        # is_unavailable() is False both when creation just succeeded and
        # when it failed with an expected BUSYGROUP (group already exists,
        # not an outage — see app/core/redis.py) — either way it's safe to
        # stop asking. Only a genuine outage (True) skips the cache, so a
        # real down-Redis keeps retrying on every subsequent poll instead
        # of being wrongly remembered as "ensured".
        if not redis_client.is_unavailable():
            _groups_ensured.add(stream)

    result = await redis_client.call(
        "xreadgroup", _GROUP, consumer, {stream: ">"}, count=_BATCH, block=_BLOCK_MS
    )
    if not result:
        return 0

    count = 0
    for _stream, messages in result:
        for message_id, fields in messages:
            count += 1
            event_id = fields.get("id", message_id)
            try:
                if not await _already_processed(event_id):
                    payload = json.loads(fields["payload"])
                    await handler(event_id, payload)
            except Exception:
                logger.warning(
                    "outbox consumer handler failed type=%s event_id=%s",
                    event_type,
                    event_id,
                    exc_info=True,
                )
                continue  # leave unacked -> redelivered later
            await redis_client.call("xack", stream, _GROUP, message_id)
    return count


async def run_consumer_loop(event_type: str, handler: Handler) -> None:
    """Background task: consume repeatedly until cancelled. A no-op loop
    (returns immediately) when the feature is toggled off. Sleeps between
    empty polls so an idle stream (the common case) doesn't busy-loop —
    `_BLOCK_MS` alone is deliberately too short to double as that pacing."""
    if not settings.OUTBOX_ENABLED:
        return
    while True:
        try:
            count = await consume_once(event_type, handler)
            if count == 0:
                await asyncio.sleep(_IDLE_SLEEP_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "outbox consumer pass failed type=%s", event_type, exc_info=True
            )
            await asyncio.sleep(1.0)
