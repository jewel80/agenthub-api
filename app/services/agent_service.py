"""Agent resolution shared by the catalog, auth, and chat paths.

One helper so every entry point agrees on agent visibility: unknown and
deactivated agents are indistinguishable (both 404).
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.repositories import agent_repo


async def get_active_main_agent_or_404(db: AsyncSession, slug: str) -> Agent:
    """Resolve an active main agent by slug (sub-agents eager-loaded).

    Raises 404 for unknown slugs AND for deactivated agents, so signup,
    login, chat, and the catalog all treat a deactivated agent as gone.
    """
    agent = await agent_repo.get_main_agent_by_slug(db, slug)
    if agent is None or not agent.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    return agent
