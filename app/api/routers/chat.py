"""Chat endpoints — one shared path for all agents/sub-agents.

Differentiation comes entirely from backend data (the resolved agent's
system_prompt). The frontend hits the same route regardless of which agent
or sub-agent the user is talking to.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user, llm_provider
from app.models.user import User
from app.repositories import message_repo
from app.schemas.chat import ChatMessageIn, ChatResponse, ChatTurnOut
from app.services import chat_engine
from app.services.llm.base import LLMProvider
from app.services.rate_limiter import get_rate_limiter

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
):
    # Cost guardrail: per-user chat rate limit.
    if not get_rate_limiter().check(str(user.id)):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Rate limit exceeded. Please slow down and try again shortly.",
        )
    reply, target, main = await chat_engine.chat(
        db,
        user,
        agent_slug=agent_slug,
        message=payload.message,
        sub_agent_slug=payload.sub_agent_slug,
        provider=provider,
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
        limit=50,
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
