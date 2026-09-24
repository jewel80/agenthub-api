"""Chat endpoints — one shared path for all agents/sub-agents.

Differentiation comes entirely from backend data (the resolved agent's
system_prompt). The frontend hits the same route regardless of which agent
or sub-agent the user is talking to.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, llm_provider, rate_limiter
from app.models.user import User
from app.repositories import message_repo
from app.schemas.chat import ChatMessageIn, ChatResponse, ChatTurnOut
from app.services import chat_engine
from app.services.llm.base import LLMProvider
from app.services.rate_limiter import RateLimiter

router = APIRouter()


@router.post(
    "/agents/{agent_slug}/chat",
    response_model=ChatResponse,
    summary="Send a message to an agent or sub-agent",
)
async def chat(
    agent_slug: str,
    payload: ChatMessageIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    provider: LLMProvider = Depends(llm_provider),
    limiter: RateLimiter = Depends(rate_limiter),
):
    # Resolve first (fix-doc F9): 401/403/404 requests must not consume the
    # caller's rate-limit budget — only real chat turns do.
    main, target = await chat_engine.resolve_target(
        db, user, agent_slug, payload.sub_agent_slug
    )

    # Cost guardrail: per-user chat rate limit.
    if not limiter.check(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded. Please slow down and try again shortly.",
            headers={"Retry-After": str(limiter.retry_after(str(user.id)))},
        )

    reply = await chat_engine.run_turn(
        db, user, main=main, target=target, message=payload.message, provider=provider
    )
    return ChatResponse(
        reply=reply,
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
