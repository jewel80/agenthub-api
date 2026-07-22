"""FastAPI dependencies: DB session, current user, LLM provider."""
from __future__ import annotations

import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import decode_access_token
from app.models.user import User
from app.repositories import user_repo
from app.services.llm import get_llm_provider
from app.services.llm.base import LLMProvider

# Bearer-token scheme. tokenUrl is informational (login is JSON); the scheme
# is only used to extract the bearer token from the Authorization header.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")


def llm_provider() -> LLMProvider:
    """Inject the configured LLM provider (cached singleton)."""
    return get_llm_provider()


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
    except jwt.PyJWTError:
        raise credentials_exc

    user_id = payload.get("sub")
    if not user_id:
        raise credentials_exc

    user = await user_repo.get_user_by_id(db, uuid.UUID(user_id))
    if user is None:
        raise credentials_exc
    return user


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
