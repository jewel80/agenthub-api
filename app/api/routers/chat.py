"""Chat endpoints — one shared path for all agents/sub-agents.

Differentiation comes entirely from backend data (the resolved agent's
system_prompt). The frontend hits the same route regardless of which agent
or sub-agent the user is talking to.

Non-streaming `POST /agents/{slug}/chat` is the original contract and stays.
`POST /v1/agents/{slug}/chat/stream` (roadmap §3) adds SSE streaming behind
the STREAMING_ENABLED toggle: same auth, scope, rate limit and quota checks.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.db import get_db
from app.core.deps import (
    get_current_user,
    llm_provider,
    rate_limiter,
    stream_db_factory,
)
from app.models.user import User
from app.repositories import message_repo
from app.schemas.chat import ChatMessageIn, ChatResponse, ChatTurnOut
from app.services import chat_engine
from app.services.chat_engine import (
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    Heartbeat,
    StartEvent,
)
from app.services.llm.base import LLMProvider
from app.services.rate_limiter import HybridRateLimiter, get_token_quota

logger = logging.getLogger("agenthub.chat")

router = APIRouter()

# Per-user concurrent stream cap (roadmap §3.4). In-process: correct for one
# instance; Redis takes over when the distributed limiter is wired (scale §2).
_stream_counts: dict[str, int] = {}


@router.post(
    "/agents/{agent_slug}/chat",
    response_model=ChatResponse,
    summary="Send a message to an agent or sub-agent",
)
async def chat(
    agent_slug: str,
    payload: ChatMessageIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    provider: LLMProvider = Depends(llm_provider),
    limiter: HybridRateLimiter = Depends(rate_limiter),
):
    # Resolve first (fix-doc F9): 401/403/404 requests must not consume the
    # caller's rate-limit budget — only real chat turns do.
    main, target = await chat_engine.resolve_target(
        db, user, agent_slug, payload.sub_agent_slug
    )

    # Cost guardrails: per-user chat rate limit (roadmap §5 headers).
    state = await limiter.hit(str(user.id))
    response.headers.update(state.headers())
    if not state.allowed:
        retry = max(state.reset_after, limiter.retry_after(str(user.id)), 1)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded. Please slow down and try again shortly.",
            headers={**state.headers(), "Retry-After": str(retry)},
        )

    result = await chat_engine.run_turn(
        db,
        user,
        main=main,
        target=target,
        message=payload.message,
        provider=provider,
    )
    return ChatResponse(
        reply=result.reply,
        agent_slug=main.slug,
        sub_agent_slug=target.slug if target.id != main.id else None,
    )


@router.get(
    "/agents/{agent_slug}/history",
    response_model=list[ChatTurnOut],
    summary="Conversation history (optionally for a sub-agent)",
)
async def history(
    agent_slug: str,
    sub_agent_slug: str | None = None,
    limit: int = Query(50, ge=1, le=100, description="Turns to return (1-100)."),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    main, target = await chat_engine.resolve_target(
        db, user, agent_slug, sub_agent_slug
    )
    sub_id = target.id if target.id != main.id else None
    rows = await message_repo.get_recent_history(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        limit=limit,
    )
    return [
        ChatTurnOut(
            id=r.id,
            role=r.role,
            content=r.content,
            created_at=r.created_at,
            sub_agent_id=r.sub_agent_id,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Streaming (roadmap §3)
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _encode_stream_event(event, message_id: str) -> str:
    if isinstance(event, Heartbeat):
        return ": ping\n\n"
    if isinstance(event, StartEvent):
        return _sse(
            "start",
            {"message_id": event.message_id, "conversation_id": event.conversation_id},
        )
    if isinstance(event, DeltaEvent):
        return _sse("delta", {"text": event.text})
    if isinstance(event, DoneEvent):
        return _sse(
            "done",
            {
                "message_id": message_id,
                "usage": {
                    "input_tokens": event.usage.input_tokens,
                    "output_tokens": event.usage.output_tokens,
                    "cache_read_tokens": event.usage.cache_read_tokens,
                },
                "stop_reason": event.stop_reason,
            },
        )
    if isinstance(event, ErrorEvent):
        return _sse("error", {"code": event.code, "message": event.message})
    raise TypeError(f"unknown stream event: {type(event).__name__}")  # pragma: no cover


@router.post(
    "/v1/agents/{agent_slug}/chat/stream",
    summary="Send a message and stream the reply (SSE)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "SSE stream: start / delta* / done | error events",
        }
    },
)
async def chat_stream(
    agent_slug: str,
    payload: ChatMessageIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    provider: LLMProvider = Depends(llm_provider),
    limiter: HybridRateLimiter = Depends(rate_limiter),
    session_factory: async_sessionmaker = Depends(stream_db_factory),
):
    if not settings.STREAMING_ENABLED:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Streaming is currently disabled.",
        )

    main, target = await chat_engine.resolve_target(
        db, user, agent_slug, payload.sub_agent_slug
    )

    state = await limiter.hit(str(user.id))
    if not state.allowed:
        retry = max(state.reset_after, limiter.retry_after(str(user.id)), 1)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded. Please slow down and try again shortly.",
            headers={**state.headers(), "Retry-After": str(retry)},
        )

    if not await get_token_quota().check(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Daily token quota exceeded. The quota resets at midnight UTC.",
            headers={"Retry-After": "3600"},
        )

    # Concurrent-stream cap per user (roadmap §3.4).
    cap = settings.MAX_CONCURRENT_STREAMS_PER_USER
    user_key = str(user.id)
    if cap > 0 and _stream_counts.get(user_key, 0) >= cap:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many concurrent streams. Please wait for one to finish.",
            headers={"Retry-After": "10"},
        )

    # Everything DB-related happens before the response starts; the stream
    # itself holds no DB connection (roadmap §3.3.1).
    prepared = await chat_engine.prepare_stream_turn(
        db, user, main=main, target=target, message=payload.message
    )

    def _bump(delta: int) -> None:
        _stream_counts[user_key] = _stream_counts.get(user_key, 0) + delta
        if _stream_counts[user_key] <= 0:
            _stream_counts.pop(user_key, None)

    _bump(1)

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in chat_engine.stream_turn(
                prepared, provider, session_factory=session_factory
            ):
                yield _encode_stream_event(event, prepared.message_id)
        finally:
            _bump(-1)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # stop nginx buffering SSE
            **state.headers(),
        },
    )
