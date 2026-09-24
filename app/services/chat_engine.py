"""THE generic chat engine — one code path for every agent.

This module is the proof that "agents are config, not code":
  1. resolve agent_slug (+optional sub_agent_slug) to a config row + system prompt
  2. enforce the user's auth scope (tenant isolation)
  3. merge conversation history (scoped to the target sub-agent thread)
  4. call the LLM through the LLMProvider interface
  5. persist the turn and return the reply

There is NO branching on which agent it is. Adding agent #101 never touches
this file — it's just a new agents row whose system_prompt drives behaviour.

Persistence decision on LLM failure: the user turn is committed *before* the
LLM call and survives it; no assistant turn is written when the provider
fails (the caller receives HTTP 503 and may retry the message).
"""
from __future__ import annotations

import logging

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import require_agent_scope
from app.models.agent import Agent
from app.models.user import User
from app.repositories import agent_repo, message_repo
from app.services.agent_service import get_active_main_agent_or_404
from app.services.llm.base import LLMMessage, LLMProvider, LLMUnavailableError
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
    # Shared helper: unknown and deactivated agents both 404 (fix-doc F4).
    main = await get_active_main_agent_or_404(db, agent_slug)

    require_agent_scope(user, main.id)  # tenant isolation

    target = main
    if sub_agent_slug:
        sub = await agent_repo.get_sub_agent_by_slug(db, main.id, sub_agent_slug)
        if sub is None or not sub.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sub-agent not found.")
        target = sub
    return main, target


async def run_turn(
    db: AsyncSession,
    user: User,
    *,
    main: Agent,
    target: Agent,
    message: str,
    provider: LLMProvider,
) -> str:
    """Run one chat turn against an already-resolved (main, target) pair.

    Transaction boundaries: the user turn is flushed *and committed* before
    the LLM call (so its `seq` precedes the reply's, and no DB transaction is
    held open across the slow provider call). The assistant turn is committed
    after a successful completion only.
    """
    sub_id = target.id if target.parent_id is not None else None

    # 1. persist the user's turn (flushed first, so its `seq` is always lower
    #    than the assistant turn that follows) and commit: the turn is the
    #    user's data and must survive an LLM outage.
    await message_repo.add_message(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        role="user",
        content=message,
    )
    await db.commit()

    # 2. load scoped history (includes the turn just persisted); pair_safe
    #    drops a leading assistant turn so the LLM context opens with `user`
    history = await message_repo.get_recent_history(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        limit=HISTORY_WINDOW,
        pair_safe=True,
    )
    llm_messages = [
        LLMMessage(role=h.role, content=h.content)
        for h in history
        if h.role in ("user", "assistant")
    ]

    # 3. call the provider with the target's system prompt (the persona);
    #    log the request WITHOUT message content (privacy + log hygiene)
    logger.info(
        "chat user=%s main=%s target=%s provider=%s history_len=%d",
        user.id, main.slug, target.slug, provider.name, len(llm_messages),
    )
    try:
        reply = await provider.complete(
            system=target.system_prompt,
            messages=llm_messages,
            max_tokens=1024,
            temperature=0.7,
        )
    except LLMUnavailableError as exc:
        # Type/status only — never the prompt or credentials. The user turn
        # stays committed; no assistant row is written.
        logger.warning(
            "llm unavailable user=%s target=%s provider=%s error=%s",
            user.id, target.slug, provider.name, exc,
        )
        raise

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
    return reply
