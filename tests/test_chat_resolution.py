"""Config-resolution tests for the chat engine.

Validates the core "agents are data" claim directly:
- agent_slug resolves to the correct system prompt
- sub_agent_slug resolves to the child's distinct system prompt
- tenant isolation blocks cross-agent resolution
- same sub-agent NAME under different parents resolves to DIFFERENT prompts
  (specialisation by parent context)
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from app.core.security import hash_password
from app.models.user import User
from app.services import chat_engine


async def _make_user(session_factory, agent_id) -> User:
    async with session_factory() as s:
        user = User(
            email=f"u-{uuid.uuid4().hex[:6]}@x.com",
            password_hash=hash_password("supersecret1"),
            agent_id=agent_id,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def test_resolve_main_agent_prompt(session_factory, rich):
    async with session_factory() as s:
        from app.models.agent import Agent
        from sqlalchemy import select

        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(
            email="resolve@x.com",
            password_hash="x",
            agent_id=doctor.id,
        )
        s.add(user)
        await s.flush()

        main, target = await chat_engine.resolve_target(s, user, "doctor-physician")
        assert target.id == main.id == doctor.id
        assert "Doctor / Physician" in target.system_prompt


async def test_resolve_sub_agent_prompt(session_factory, rich):
    async with session_factory() as s:
        from app.models.agent import Agent
        from sqlalchemy import select

        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(email="resolve2@x.com", password_hash="x", agent_id=doctor.id)
        s.add(user)
        await s.flush()

        main, target = await chat_engine.resolve_target(
            s, user, "doctor-physician", "doctor-physician-clinical-advisor-agent"
        )
        assert target.id != main.id
        assert target.parent_id == main.id
        assert "Clinical Advisor" in target.system_prompt
        # sub-agent prompt is genuinely different from the main agent prompt
        assert target.system_prompt != main.system_prompt


async def test_cross_agent_resolution_forbidden(session_factory, rich):
    async with session_factory() as s:
        from app.models.agent import Agent
        from sqlalchemy import select

        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(email="isolation@x.com", password_hash="x", agent_id=doctor.id)
        s.add(user)
        await s.flush()

        with pytest.raises(HTTPException) as exc:
            await chat_engine.resolve_target(s, user, "corporate-lawyer")
        assert exc.value.status_code == 403


async def test_same_subagent_name_different_parent_different_prompt(
    session_factory, rich
):
    """'Learning Agent' under Doctor vs under Lawyer -> different system prompts.

    This is the architecture requirement made concrete: the same sub-agent NAME
    resolves to genuinely different, parent-specialised behaviour.
    """
    async with session_factory() as s:
        from app.models.agent import Agent
        from sqlalchemy import select

        doc_learn = (
            await s.execute(
                select(Agent).where(Agent.slug == "doctor-physician-learning-agent")
            )
        ).scalars().one()
        law_learn = (
            await s.execute(
                select(Agent).where(Agent.slug == "corporate-lawyer-learning-agent")
            )
        ).scalars().one()

        assert doc_learn.profession == law_learn.profession == "Learning Agent"
        assert doc_learn.system_prompt != law_learn.system_prompt
        assert "Doctor" in doc_learn.system_prompt
        assert "Corporate Lawyer" in law_learn.system_prompt


async def test_resolve_unknown_agent_404(session_factory, rich):
    async with session_factory() as s:
        from app.models.agent import Agent
        from sqlalchemy import select

        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(email="u404@x.com", password_hash="x", agent_id=doctor.id)
        s.add(user)
        await s.flush()

        with pytest.raises(HTTPException) as exc:
            await chat_engine.resolve_target(s, user, "nope")
        assert exc.value.status_code == 404
