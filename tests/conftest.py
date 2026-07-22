"""Shared pytest fixtures.

Tests run against an in-memory SQLite DB (StaticPool => single shared
connection so all sessions see seeded data) with the real FastAPI app via
httpx ASGITransport. No external services required.
"""
from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.core.deps import llm_provider
from app.main import app
from app.models import Base
from app.models.agent import Agent
from app.services.llm.mock_provider import MockProvider


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def client(session_factory):
    """Async HTTP client wired to the app with the test DB injected."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    # Always inject the deterministic mock LLM in tests (no API key needed).
    app.dependency_overrides[llm_provider] = lambda: MockProvider()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def seeded(session_factory):
    """Two main agents (A=Doctor, B=Lawyer) for isolation tests."""
    async with session_factory() as s:
        s.add_all(
            [
                Agent(
                    slug="doctor-physician",
                    industry="Healthcare",
                    profession="Doctor / Physician",
                    tagline="Your AI doctor",
                    description="d",
                    system_prompt="You are a senior Doctor / Physician.",
                    parent_id=None,
                    is_featured=True,
                ),
                Agent(
                    slug="corporate-lawyer",
                    industry="Legal Services",
                    profession="Corporate Lawyer",
                    tagline="Your AI lawyer",
                    description="d",
                    system_prompt="You are a senior Corporate Lawyer.",
                    parent_id=None,
                    is_featured=True,
                ),
            ]
        )
        await s.commit()


@pytest_asyncio.fixture
async def rich(session_factory):
    """Doctor (2 sub-agents) + Lawyer (1 sub-agent) — incl. a same-named
    'Learning Agent' under each, to prove sub-agent differentiation by parent."""
    async with session_factory() as s:
        doctor = Agent(
            slug="doctor-physician",
            industry="Healthcare",
            profession="Doctor / Physician",
            tagline="t",
            description="d",
            system_prompt="You are a senior Doctor / Physician in Healthcare.",
            parent_id=None,
            is_featured=True,
        )
        lawyer = Agent(
            slug="corporate-lawyer",
            industry="Legal Services",
            profession="Corporate Lawyer",
            tagline="t",
            description="d",
            system_prompt="You are a senior Corporate Lawyer in Legal Services.",
            parent_id=None,
            is_featured=True,
        )
        s.add_all([doctor, lawyer])
        await s.flush()

        s.add_all(
            [
                Agent(
                    slug="doctor-physician-clinical-advisor-agent",
                    industry="Healthcare",
                    profession="Clinical Advisor Agent",
                    tagline="t",
                    description="d",
                    system_prompt=(
                        'You are "Clinical Advisor Agent", a specialised sub-agent '
                        "for a Doctor / Physician in Healthcare. Focus: clinical advice."
                    ),
                    parent_id=doctor.id,
                    sort_order=1,
                ),
                Agent(
                    slug="doctor-physician-learning-agent",
                    industry="Healthcare",
                    profession="Learning Agent",
                    tagline="t",
                    description="d",
                    system_prompt=(
                        'You are "Learning Agent", a specialised sub-agent for a '
                        "Doctor / Physician in Healthcare. Focus: medical learning."
                    ),
                    parent_id=doctor.id,
                    sort_order=2,
                ),
                Agent(
                    slug="corporate-lawyer-learning-agent",
                    industry="Legal Services",
                    profession="Learning Agent",
                    tagline="t",
                    description="d",
                    system_prompt=(
                        'You are "Learning Agent", a specialised sub-agent for a '
                        "Corporate Lawyer in Legal Services. Focus: legal learning."
                    ),
                    parent_id=lawyer.id,
                    sort_order=1,
                ),
            ]
        )
        await s.commit()
