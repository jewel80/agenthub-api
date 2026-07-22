"""Authentication endpoints — signup/login scoped to a chosen agent.

Routes are parameterised by the agent's slug, so a credential is always bound
to one agent: `POST /agents/{agent_slug}/signup`, `POST /agents/{agent_slug}/login`.
An account created under Agent A does NOT exist under Agent B.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent
from app.models.user import User
from app.repositories import agent_repo
from app.schemas.auth import LoginIn, SignupIn, TokenOut, UserOut
from app.services import auth_service

router = APIRouter()


async def _resolve_main_agent(slug: str, db: AsyncSession) -> Agent:
    agent = await agent_repo.get_main_agent_by_slug(db, slug)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found.")
    return agent


@router.post(
    "/agents/{agent_slug}/signup",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Sign up scoped to an agent",
)
async def signup(
    agent_slug: str,
    payload: SignupIn,
    db: AsyncSession = Depends(get_db),
):
    agent = await _resolve_main_agent(agent_slug, db)
    _, token = await auth_service.signup(
        db, agent, email=payload.email, password=payload.password
    )
    return token


@router.post(
    "/agents/{agent_slug}/login",
    response_model=TokenOut,
    summary="Log in scoped to an agent",
)
async def login(
    agent_slug: str,
    payload: LoginIn,
    db: AsyncSession = Depends(get_db),
):
    agent = await _resolve_main_agent(agent_slug, db)
    return await auth_service.login(
        db, agent, email=payload.email, password=payload.password
    )


@router.get("/me", response_model=UserOut, summary="Current user")
async def me(current_user: User = Depends(get_current_user)):
    return current_user
