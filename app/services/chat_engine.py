"""THE generic chat engine — one code path for every agent.

This module is the proof that "agents are config, not code":
  1. resolve agent_slug (+optional sub_agent_slug) to a config row + system prompt
  2. enforce the user's auth scope (tenant isolation)
  3. merge conversation history (scoped to the target sub-agent thread)
  4. call the LLM through the LLMProvider interface
  5. persist the turn and return the reply

There is NO branching on which agent it is. Adding agent #101 never touches
this file — it's just a new agents row whose system_prompt drives behaviour.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_agent_scope
from app.models.agent import Agent
from app.models.user import User
from app.repositories import agent_repo, message_repo
from app.services.llm.base import LLMMessage, LLMProvider
from app.services.observability import get_usage_tracker

logger = logging.getLogger("agenthub.chat")

# Conversation window retained per request (keeps token cost bounded).
HISTORY_WINDOW = 20


async def resolve_target(
    db: AsyncSession,
    user: User,
    agent_slug: str,
    sub_agent_slug: str | None = None,
) -> tuple[Agent, Agent]:
    """Resolve (main_agent, target_agent) and enforce the user's scope.

    `target` is the sub-agent if `sub_agent_slug` is given, else the main agent.
    Raises 403 if the user's token agent_id doesn't own this resource.
    """
    main = await agent_repo.get_main_agent_by_slug(db, agent_slug)
    if main is None or not main.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")

    require_agent_scope(user, main.id)  # tenant isolation

    target = main
    if sub_agent_slug:
        sub = await agent_repo.get_sub_agent_by_slug(db, main.id, sub_agent_slug)
        if sub is None or not sub.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sub-agent not found.")
        target = sub
    return main, target


async def chat(
    db: AsyncSession,
    user: User,
    *,
    agent_slug: str,
    message: str,
    sub_agent_slug: str | None,
    provider: LLMProvider,
) -> tuple[str, Agent, Agent]:
    """Run one chat turn. Returns (reply, target_agent, main_agent)."""
    main, target = await resolve_target(db, user, agent_slug, sub_agent_slug)
    sub_id = target.id if target.parent_id is not None else None

    # 1. persist the user's turn
    await message_repo.add_message(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        role="user",
        content=message,
    )

    # 2. load scoped history (includes the turn just persisted)
    history = await message_repo.get_recent_history(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        limit=HISTORY_WINDOW,
    )
    llm_messages = [
        LLMMessage(role=h.role, content=h.content)
        for h in history
        if h.role in ("user", "assistant")
    ]

    # 3. call the provider with the target's system prompt (the persona)
    logger.info(
        "chat user=%s main=%s target=%s provider=%s",
        user.id, main.slug, target.slug, provider.name,
    )
    reply = await provider.complete(
        system=target.system_prompt,
        messages=llm_messages,
        max_tokens=1024,
        temperature=0.7,
    )

    # 4. persist the assistant's reply
    await message_repo.add_message(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        role="assistant",
        content=reply,
    )
    await db.commit()

    # basic observability: which agent/sub-agent got used
    get_usage_tracker().record(
        main.slug, target.slug if target.id != main.id else None
    )
    return reply, target, main
