"""Shared pytest fixtures.

Tests run against **PostgreSQL** via TEST_DATABASE_URL (never the main DB):
the schema is created once per session with `alembic upgrade head`, and all
tables are truncated after every test for isolation. The app under test is
the real FastAPI app via httpx ASGITransport, with the deterministic mock LLM
provider injected (no API key needed).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.core.config import settings
from app.core.db import build_connect_args, get_db
from app.core.deps import llm_provider
from app.main import app
from app.models.agent import Agent
from app.services.llm.mock_provider import MockProvider

_REPO_ROOT = Path(__file__).resolve().parents[1]

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL") or settings.TEST_DATABASE_URL

# Tables truncated between tests (CASCADE resolves FK order).
_ALL_TABLES = ("messages", "users", "agents")


def _require_test_db() -> None:
    if not TEST_DB_URL.startswith("postgresql+asyncpg://"):
        pytest.exit(
            "TEST_DATABASE_URL must point at a disposable PostgreSQL database\n"
            "(postgresql+asyncpg://user:pass@host:port/dbname).\n"
            "Tests TRUNCATE tables — never point this at your main database.\n"
            "Add it to .env or the environment before running pytest.",
            returncode=3,
        )


@pytest.fixture(scope="session", autouse=True)
def _run_migrations():
    """Create the schema once per session on the test database."""
    _require_test_db()
    os.environ["ALEMBIC_DATABASE_URL"] = TEST_DB_URL
    alembic_cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(_REPO_ROOT / "alembic"))
    command.upgrade(alembic_cfg, "head")


@pytest_asyncio.fixture
async def engine():
    """Per-test engine against the test DB; wipes all data afterwards."""
    eng = create_async_engine(
        TEST_DB_URL, pool_pre_ping=True, connect_args=build_connect_args()
    )
    yield eng
    async with eng.begin() as conn:
        for table in _ALL_TABLES:
            await conn.execute(
                text(f'TRUNCATE TABLE "{table}" RESTART IDENTITY CASCADE')
            )
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
                        "for a Doctor / Physician in Healthcare. "
                        "Focus: clinical advice."
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
