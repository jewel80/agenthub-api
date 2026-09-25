"""Roadmap §3 — streaming chat tests.

Covers: SSE event order, persistence rules (complete/interrupted/failed),
the zero-text-failure rule, the feature toggle, and the concurrent-stream
cap. Disconnect is exercised at the engine level (agen.aclose), which is
the exact code path Starlette drives on client disconnect.
"""
from __future__ import annotations

import asyncio
import json

from app.core.deps import llm_provider, stream_db_factory
from app.main import app
from app.services import chat_engine
from app.services.llm.base import (
    LLMProvider,
    LLMStreamDelta,
    LLMStreamDone,
    LLMUnavailableError,
    LLMUsage,
)
from app.services.llm.mock_provider import MockProvider

EMAIL = "streamer@example.com"
PASSWORD = "supersecret1"

STREAM_URL = "/v1/agents/doctor-physician/chat/stream"


async def _signup(client) -> str:
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _parse_sse(raw: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if event:
            events.append((event, data))
    return events


class ChunkThenFailProvider(LLMProvider):
    """Yields two deltas, then fails like an unreachable LLM."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "chunk-then-fail"

    async def complete(
        self, *, system, messages, max_tokens=1024, temperature=0.7
    ) -> str:
        raise LLMUnavailableError("anthropic unavailable: APIStatusError")

    async def stream_complete(self, *, system, messages, **_kw):
        yield LLMStreamDelta(text="partial ")
        yield LLMStreamDelta(text="reply")
        raise LLMUnavailableError("anthropic unavailable: APIStatusError")


class FailImmediatelyProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "fail-immediately"

    async def complete(
        self, *, system, messages, max_tokens=1024, temperature=0.7
    ) -> str:
        raise LLMUnavailableError("anthropic unavailable: APITimeoutError")

    async def stream_complete(self, *, system, messages, **_kw):
        raise LLMUnavailableError("anthropic unavailable: APITimeoutError")
        yield  # pragma: no cover (makes this an async generator)


async def _history(client, token) -> list[dict]:
    r = await client.get(
        "/agents/doctor-physician/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def test_stream_happy_path_event_order_and_persistence(
    client, rich, session_factory
):
    app.dependency_overrides[stream_db_factory] = lambda: session_factory
    token = await _signup(client)
    headers = {"Authorization": f"Bearer {token}"}

    async with client.stream(
        "POST", STREAM_URL, json={"message": "hello stream"}, headers=headers
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"
        raw = (await resp.aread()).decode()

    events = _parse_sse(raw)
    names = [e for e, _ in events]
    assert names[0] == "start"
    assert names[-1] == "done"
    assert "delta" in names

    start_data = next(d for e, d in events if e == "start")
    done_data = next(d for e, d in events if e == "done")
    assert start_data["message_id"] == done_data["message_id"]
    assert start_data["conversation_id"]
    assert done_data["usage"]["output_tokens"] >= 0
    assert done_data["stop_reason"] == "end_turn"

    # assistant turn persisted as complete; user turn also present
    turns = await _history(client, token)
    assert [t["role"] for t in turns] == ["user", "assistant"]


async def test_stream_persists_complete_status(client, rich, session_factory):
    from sqlalchemy import select

    from app.models.message import Message

    app.dependency_overrides[stream_db_factory] = lambda: session_factory
    token = await _signup(client)
    async with client.stream(
        "POST",
        STREAM_URL,
        json={"message": "status check"},
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        await resp.aread()
    async with session_factory() as s:
        rows = (await s.execute(
            select(Message).where(Message.role == "assistant")
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "complete"


async def test_stream_midstream_failure_saves_partial_as_failed(
    client, rich, session_factory
):
    from sqlalchemy import select

    from app.models.message import Message

    app.dependency_overrides[stream_db_factory] = lambda: session_factory
    app.dependency_overrides[llm_provider] = lambda: ChunkThenFailProvider()
    try:
        token = await _signup(client)
        async with client.stream(
            "POST", STREAM_URL, json={"message": "will fail"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            assert resp.status_code == 200
            raw = (await resp.aread()).decode()
    finally:
        app.dependency_overrides[llm_provider] = lambda: MockProvider()

    events = _parse_sse(raw)
    names = [e for e, _ in events]
    assert names == ["start", "delta", "delta", "error"]
    err = events[-1][1]
    assert err["code"] == "llm_unavailable"

    async with session_factory() as s:
        row = (await s.execute(
            select(Message).where(Message.role == "assistant")
        )).scalars().one()
        assert row.status == "failed"
        assert row.content == "partial reply"


async def test_stream_immediate_failure_writes_no_assistant_row(
    client, rich, session_factory
):
    from sqlalchemy import select

    from app.models.message import Message

    app.dependency_overrides[stream_db_factory] = lambda: session_factory
    app.dependency_overrides[llm_provider] = lambda: FailImmediatelyProvider()
    try:
        token = await _signup(client)
        async with client.stream(
            "POST", STREAM_URL, json={"message": "no reply at all"},
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            assert resp.status_code == 200
            raw = (await resp.aread()).decode()
    finally:
        app.dependency_overrides[llm_provider] = lambda: MockProvider()

    events = _parse_sse(raw)
    assert [e for e, _ in events] == ["start", "error"]
    async with session_factory() as s:
        rows = (await s.execute(
            select(Message).where(Message.role == "assistant")
        )).scalars().all()
        assert rows == []  # zero-text failure persists nothing
    turns = await _history(client, token)
    assert [t["role"] for t in turns] == ["user"]  # user turn survived


async def test_stream_disabled_toggle(client, rich, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "STREAMING_ENABLED", False)
    token = await _signup(client)
    r = await client.post(
        STREAM_URL, json={"message": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 503, r.text


async def test_stream_requires_auth(client, rich):
    r = await client.post(STREAM_URL, json={"message": "hi"})
    assert r.status_code == 401


async def test_stream_concurrent_cap(client, rich, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "MAX_CONCURRENT_STREAMS_PER_USER", 1)
    token = await _signup(client)
    headers = {"Authorization": f"Bearer {token}"}

    # occupy the single slot directly (simulates one active stream)
    from app.api.routers import chat as chat_router

    user_id = (await client.get("/me", headers=headers)).json()["id"]
    chat_router._stream_counts[user_id] = 1
    try:
        r = await client.post(STREAM_URL, json={"message": "second"}, headers=headers)
        assert r.status_code == 429, r.text
        assert r.headers.get("Retry-After")
        assert "concurrent" in r.json()["detail"].lower()
    finally:
        chat_router._stream_counts.clear()


# ---------------------------------------------------------------------------
# Engine-level disconnect (client aborts mid-stream)
# ---------------------------------------------------------------------------


class SlowChunksProvider(LLMProvider):
    """Yields several deltas with pauses so a consumer can abort mid-way."""

    @property
    def name(self) -> str:
        return "slow-chunks"

    async def complete(
        self, *, system, messages, max_tokens=1024, temperature=0.7
    ) -> str:
        return "slow"

    async def stream_complete(self, *, system, messages, **_kw):
        for word in ("one ", "two ", "three ", "four "):
            yield LLMStreamDelta(text=word)
        yield LLMStreamDone(usage=LLMUsage(input_tokens=1, output_tokens=4))


class ConcurrencyTrackingProvider(LLMProvider):
    """Records how many `stream_complete` calls were in flight at once."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    @property
    def name(self) -> str:
        return "concurrency-tracking"

    async def complete(
        self, *, system, messages, max_tokens=1024, temperature=0.7
    ) -> str:
        raise NotImplementedError

    async def stream_complete(self, *, system, messages, **_kw):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            yield LLMStreamDelta(text="chunk")
            await asyncio.sleep(0.05)
        finally:
            self.active -= 1
        yield LLMStreamDone(usage=LLMUsage())


async def test_stream_respects_global_llm_concurrency_cap(
    client, rich, session_factory, monkeypatch
):
    """GLOBAL_LLM_CONCURRENCY must gate streaming calls too, not just the
    non-streaming path — two concurrent streams never both hold the LLM
    slot when the cap is 1 (roadmap §5)."""
    from sqlalchemy import select as sa_select

    from app.core.config import settings
    from app.models.agent import Agent
    from app.models.user import User
    from app.services import rate_limiter as rate_limiter_mod

    monkeypatch.setattr(settings, "GLOBAL_LLM_CONCURRENCY", 1)
    rate_limiter_mod._llm_semaphore = None
    await _signup(client)

    async with session_factory() as s:
        main = (await s.execute(
            sa_select(Agent).where(Agent.slug == "doctor-physician")
        )).scalars().one()
        user = (await s.execute(
            sa_select(User).where(User.email == EMAIL)
        )).scalars().one()
        prepared_a = await chat_engine.prepare_stream_turn(
            s, user, main=main, target=main, message="first"
        )
        prepared_b = await chat_engine.prepare_stream_turn(
            s, user, main=main, target=main, message="second"
        )

    provider = ConcurrencyTrackingProvider()

    async def _drain(prepared):
        async for _event in chat_engine.stream_turn(
            prepared, provider, session_factory=session_factory
        ):
            pass

    try:
        await asyncio.gather(_drain(prepared_a), _drain(prepared_b))
        assert provider.max_active == 1
    finally:
        rate_limiter_mod._llm_semaphore = None  # don't leak cap=1 to later tests


async def test_stream_disconnect_persists_interrupted(
    client, rich, session_factory
):
    from sqlalchemy import select

    from app.models.message import Message

    await _signup(client)

    # Build the prepared context properly (needs user/main/target rows).
    from sqlalchemy import select as sa_select

    from app.models.agent import Agent
    from app.models.user import User

    async with session_factory() as s:
        main = (await s.execute(
            sa_select(Agent).where(Agent.slug == "doctor-physician")
        )).scalars().one()
        user = (await s.execute(
            sa_select(User).where(User.email == EMAIL)
        )).scalars().one()
        prepared = await chat_engine.prepare_stream_turn(
            s, user, main=main, target=main, message="abort me"
        )

    provider = SlowChunksProvider()
    agen = chat_engine.stream_turn(
        prepared, provider, session_factory=session_factory, max_seconds=30
    )
    seen: list[str] = []
    async for event in agen:
        if isinstance(event, chat_engine.DeltaEvent):
            seen.append(event.text)
        if len(seen) == 2:
            break  # consumer goes away mid-stream
    await agen.aclose()  # exactly what Starlette does on client disconnect

    async with session_factory() as s:
        row = (await s.execute(
            select(Message).where(Message.role == "assistant")
        )).scalars().one()
        assert row.status == "interrupted"
        assert "".join(seen) in row.content or row.content.startswith("one ")
