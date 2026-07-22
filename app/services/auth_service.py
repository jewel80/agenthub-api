"""Authentication business logic — tenant-scoped per agent.

Signup and login are ALWAYS scoped to a specific agent. The same email may
register independently under different agents; a credential set is only valid
within the agent it was created for.
"""
from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password, verify_password
from app.models.agent import Agent
from app.models.user import User
from app.repositories import user_repo
from app.schemas.auth import TokenOut


async def signup(
    db: AsyncSession, agent: Agent, *, email: str, password: str
) -> tuple[User, TokenOut]:
    existing = await user_repo.get_user_by_email_and_agent(db, email, agent.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists for this agent.",
        )
    user = await user_repo.create_user(
        db, email=email, password_hash=hash_password(password), agent_id=agent.id
    )
    await db.commit()
    await db.refresh(user)
    return user, _issue_token(user, agent)


async def login(
    db: AsyncSession, agent: Agent, *, email: str, password: str
) -> TokenOut:
    user = await user_repo.get_user_by_email_and_agent(db, email, agent.id)
    # Identical message whether the account is missing or the password is wrong
    # (avoid user-enumeration leakage).
    if user is None or not verify_password(password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password for this agent.",
        )
    return _issue_token(user, agent)


def _issue_token(user: User, agent: Agent) -> TokenOut:
    token = create_access_token(sub=str(user.id), agent_id=user.agent_id)
    return TokenOut(
        access_token=token,
        agent_id=agent.id,
        agent_slug=agent.slug,
        agent_profession=agent.profession,
    )
