"""FastAPI dependencies: DB session, current user, LLM provider, rate limiter."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import db as core_db
from app.core.db import AsyncSessionLocal, get_db
from app.core.security import decode_access_token
from app.models.user import User
from app.repositories import user_repo
from app.services.llm import get_llm_provider
from app.services.llm.base import LLMProvider
from app.services.rate_limiter import HybridRateLimiter, get_rate_limiter

# Bearer-token scheme. tokenUrl is informational (login is JSON); the scheme
# is only used to extract the bearer token from the Authorization header.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def llm_provider() -> LLMProvider:
    """Inject the configured LLM provider (cached singleton)."""
    return get_llm_provider()


def rate_limiter() -> HybridRateLimiter:
    """Inject the rate limiter (cached singleton; overridable in tests)."""
    return get_rate_limiter()


def stream_db_factory() -> async_sessionmaker:
    """Session factory for streaming persistence (overridable in tests).

    The streaming endpoint must not hold the request's DB session open for
    the stream duration; it writes the assistant turn in short dedicated
    sessions from this factory.
    """
    return AsyncSessionLocal


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise credentials_exc from exc

    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exc

    # A signed token with a non-UUID `sub` is malformed -> 401, not 500 (F10).
    try:
        user_uuid = uuid.UUID(str(user_id))
    except ValueError:
        raise credentials_exc from None

    user = await user_repo.get_user_by_id(db, user_uuid)
    if user is None:
        raise credentials_exc

    # The agent_id claim must still match the account's binding: a token
    # re-bound to another agent is treated as invalid (fix-doc F12).
    token_agent_id = payload.get("agent_id")
    if token_agent_id is not None and str(user.agent_id) != str(token_agent_id):
        raise credentials_exc
    return user


async def get_read_db_for_user(
    user: User = Depends(get_current_user),
) -> AsyncIterator[AsyncSession]:
    """Read-only session with read-your-writes (scale-doc §1.2.5): if `user`
    wrote recently, returns a writer session so they see their own write
    immediately (e.g. history right after posting a chat turn); otherwise
    the normal reader path (`app.core.db.get_read_db`).

    Depending on `get_current_user` (rather than taking a raw user id)
    means FastAPI resolves it once per request and the endpoint's own
    `user` param and this session share that same resolution — no double
    auth/DB lookup.
    """
    if await core_db.read_your_writes_active(user.id):
        async with AsyncSessionLocal() as session:
            yield session
        return
    # Delegate to get_read_db() for the normal path so the replica-health
    # fail-over logic lives in exactly one place.
    async for session in core_db.get_read_db():
        yield session


def require_agent_scope(current_user: User, agent_id: uuid.UUID) -> None:
    """Enforce tenant isolation: the user's token agent_id must own the resource.

    Called by protected endpoints with the requested resource's owning main
    agent id. A user authenticated under Agent A is rejected for Agent B.
    """
    if current_user.agent_id != agent_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is not scoped to the requested agent.",
        )
