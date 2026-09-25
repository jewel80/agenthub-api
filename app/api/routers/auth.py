"""Authentication endpoints — signup/login scoped to a chosen agent.

Routes are parameterised by the agent's slug, so a credential is always bound
to one agent: `POST /agents/{agent_slug}/signup`, `POST /agents/{agent_slug}/login`.
An account created under Agent A does NOT exist under Agent B.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.models.agent import Agent
from app.models.user import User
from app.schemas.auth import LoginIn, SignupIn, TokenOut, UserOut
from app.services import agent_service, auth_service
from app.services.login_guard import get_login_guard
from app.services.rate_limiter import get_login_rate_limiter

router = APIRouter()


async def _resolve_main_agent(slug: str, db: AsyncSession) -> Agent:
    # Shared helper: unknown and deactivated agents both 404 (fix-doc F4).
    return await agent_service.get_active_main_agent_or_404(db, slug)


async def _enforce_login_rate_limit(key: str) -> None:
    """Stricter per-(email, agent) request rate on login attempts (roadmap
    §5), on top of LoginGuard's failure-triggered lockout. Signup abuse is
    covered separately by the per-IP limiter (fix-doc F5 handles the
    duplicate-insert race) and the account-creation flow doesn't need the
    same brute-force defense as password guessing."""
    limiter = get_login_rate_limiter()
    state = await limiter.hit(key)
    if not state.allowed:
        retry = max(state.reset_after, limiter.retry_after(key), 1)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many attempts. Please slow down and try again shortly.",
            headers={**state.headers(), "Retry-After": str(retry)},
        )


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
    guard_key = f"{payload.email}|{agent.slug}"
    await _enforce_login_rate_limit(guard_key)
    # Brute-force lockout (roadmap §5): repeated failures lock the
    # (email, agent) key exponentially — even a correct password is
    # refused while locked.
    guard = get_login_guard()
    locked_for = guard.retry_after(guard_key)
    if locked_for is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed attempts. Try again later.",
            headers={"Retry-After": str(locked_for)},
        )
    try:
        token = await auth_service.login(
            db, agent, email=payload.email, password=payload.password
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            guard.record_failure(guard_key)
        raise
    guard.record_success(guard_key)
    return token


@router.get("/me", response_model=UserOut, summary="Current user")
async def me(current_user: User = Depends(get_current_user)):
    return current_user
