"""Authentication endpoints — signup/login scoped to a chosen agent.

Routes are parameterised by the agent's slug, so a credential is always bound
to one agent: `POST /agents/{agent_slug}/signup`, `POST /agents/{agent_slug}/login`.
An account created under Agent A does NOT exist under Agent B.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent
from app.models.user import User
from app.schemas.auth import LoginIn, SignupIn, TokenOut, UserOut
from app.services import agent_service, auth_service

router = APIRouter()


async def _resolve_main_agent(slug: str, db: AsyncSession) -> Agent:
    # Shared helper: unknown and deactivated agents both 404 (fix-doc F4).
    return await agent_service.get_active_main_agent_or_404(db, slug)


@router.post(
    "/agents/{agent_slug}/signup",
    response_model=TokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Sign up scoped to an agent",
)
async def signup(
    agent_slug: str,
    payload: SignupIn,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    agent = await _resolve_main_agent(agent_slug, db)
    _, token, created = await auth_service.signup(
        db, agent, email=payload.email, password=payload.password
    )
    # Idempotent: an existing account with the correct password gets a token as
    # a login (200 OK), not a fresh creation (201 Created).
    if not created:
        response.status_code = status.HTTP_200_OK
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
