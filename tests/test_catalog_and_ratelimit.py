"""F8 — SQL catalog filtering · F9 — rate-limit order + Retry-After ·
F10 — malformed JWT `sub`.

F8 asserts the same results the previous Python-side filtering produced
(contract unchanged), with case-insensitive matching moved into SQL.
"""
from __future__ import annotations

import jwt
import pytest

from app.core.config import settings
from app.core.deps import rate_limiter as rate_limiter_dep
from app.main import app
from app.services.rate_limiter import RateLimiter

EMAIL = "filter@example.com"
PASSWORD = "supersecret1"


# ---------------------------------------------------------------------------
# F8 — catalog filtering in SQL
# ---------------------------------------------------------------------------


async def test_catalog_unfiltered_returns_all(client, seeded):
    r = await client.get("/agents")
    assert r.status_code == 200
    slugs = [a["slug"] for a in r.json()]
    assert set(slugs) == {"doctor-physician", "corporate-lawyer"}


async def test_catalog_industry_filter_case_insensitive(client, seeded):
    r = await client.get("/agents", params={"industry": "healthcare"})
    assert r.status_code == 200
    assert [a["slug"] for a in r.json()] == ["doctor-physician"]


async def test_catalog_q_matches_profession(client, seeded):
    r = await client.get("/agents", params={"q": "doctor"})
    assert {a["slug"] for a in r.json()} == {"doctor-physician"}


async def test_catalog_q_matches_industry(client, seeded):
    r = await client.get("/agents", params={"q": "legal"})
    assert {a["slug"] for a in r.json()} == {"corporate-lawyer"}


async def test_catalog_featured_filter(client, seeded):
    r = await client.get("/agents", params={"featured": True})
    assert {a["slug"] for a in r.json()} == {
        "doctor-physician",
        "corporate-lawyer",
    }  # both seeded agents are featured
    r = await client.get("/agents", params={"featured": False})
    assert r.json() == []


async def test_catalog_limit_offset(client, seeded):
    page1 = await client.get("/agents", params={"limit": 1})
    assert len(page1.json()) == 1
    first_slug = page1.json()[0]["slug"]
    page2 = await client.get("/agents", params={"limit": 1, "offset": 1})
    assert len(page2.json()) == 1
    assert page2.json()[0]["slug"] != first_slug


async def test_catalog_counts_active_sub_agents_only(client, rich, session_factory):
    from sqlalchemy import update

    from app.models.agent import Agent

    r = await client.get("/agents/doctor-physician")
    assert len(r.json()["sub_agents"]) == 2

    async with session_factory() as s:
        await s.execute(
            update(Agent)
            .where(Agent.slug == "doctor-physician-clinical-advisor-agent")
            .values(is_active=False)
        )
        await s.commit()

    r = await client.get("/agents")
    doctor = next(a for a in r.json() if a["slug"] == "doctor-physician")
    assert doctor["sub_agent_count"] == 1


async def test_industries_sorted_distinct(client, seeded):
    r = await client.get("/industries")
    assert r.status_code == 200
    assert r.json() == ["Healthcare", "Legal Services"]


# ---------------------------------------------------------------------------
# F9 — rate limit: Retry-After header; 403/404 don't consume budget
# ---------------------------------------------------------------------------


async def _signup(client, slug: str = "doctor-physician") -> str:
    r = await client.post(
        f"/agents/{slug}/signup", json={"email": EMAIL, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


@pytest.fixture
def tight_limiter():
    limiter = RateLimiter(max_per_min=2)
    # One instance for the whole test: a fresh limiter per request would
    # never accumulate hits and could never return 429.
    app.dependency_overrides[rate_limiter_dep] = lambda: limiter
    yield limiter
    app.dependency_overrides.pop(rate_limiter_dep, None)


async def test_429_includes_retry_after(client, rich, tight_limiter):
    token = await _signup(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        await client.post(
            "/agents/doctor-physician/chat", json={"message": "1"}, headers=headers
        )
    ).status_code == 200
    assert (
        await client.post(
            "/agents/doctor-physician/chat", json={"message": "2"}, headers=headers
        )
    ).status_code == 200
    r = await client.post(
        "/agents/doctor-physician/chat", json={"message": "3"}, headers=headers
    )
    assert r.status_code == 429, r.text
    retry_after = r.headers.get("Retry-After")
    assert retry_after is not None and retry_after.isdigit()
    assert 1 <= int(retry_after) <= 60


async def test_404_and_403_do_not_consume_budget(client, rich, tight_limiter):
    token = await _signup(client)
    headers = {"Authorization": f"Bearer {token}"}
    # unknown agent -> 404 (resolution fails before the limiter)
    assert (
        await client.post(
            "/agents/no-such-agent/chat", json={"message": "x"}, headers=headers
        )
    ).status_code == 404
    # cross-agent -> 403 (scope enforced before the limiter)
    assert (
        await client.post(
            "/agents/corporate-lawyer/chat", json={"message": "x"}, headers=headers
        )
    ).status_code == 403
    # both real chats still allowed: budget untouched by 404/403
    assert (
        await client.post(
            "/agents/doctor-physician/chat", json={"message": "1"}, headers=headers
        )
    ).status_code == 200
    assert (
        await client.post(
            "/agents/doctor-physician/chat", json={"message": "2"}, headers=headers
        )
    ).status_code == 200


# ---------------------------------------------------------------------------
# F10 — malformed JWT sub
# ---------------------------------------------------------------------------


async def test_signed_token_with_non_uuid_sub_returns_401(client, seeded):
    payload = {
        "sub": "not-a-uuid",
        "agent_id": "00000000-0000-0000-0000-000000000000",
        "exp": 9999999999,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)
    r = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text  # not a 500
