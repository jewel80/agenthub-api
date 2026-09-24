"""F4 — deactivated agents are invisible everywhere (same 404 as unknown).

Auth (signup/login), chat, catalog detail, and the catalog list must all
agree: a deactivated agent is gone.
"""
from __future__ import annotations

from sqlalchemy import update

from app.models.agent import Agent

EMAIL = "ghost@example.com"
PASSWORD = "supersecret1"


async def _deactivate(session_factory, slug: str, sub: bool = False) -> None:
    async with session_factory() as s:
        await s.execute(
            update(Agent).where(Agent.slug == slug).values(is_active=False)
        )
        await s.commit()


async def test_deactivated_agent_detail_404(client, rich, session_factory):
    await _deactivate(session_factory, "doctor-physician")
    r = await client.get("/agents/doctor-physician")
    assert r.status_code == 404, r.text
    # indistinguishable from an unknown slug (request_id differs by design)
    other = await client.get("/agents/no-such-agent")
    assert other.status_code == 404
    assert r.json()["detail"] == other.json()["detail"]


async def test_deactivated_agent_signup_404(client, rich, session_factory):
    await _deactivate(session_factory, "doctor-physician")
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 404, r.text


async def test_deactivated_agent_login_404(client, rich, session_factory):
    # account created while the agent was active
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    await _deactivate(session_factory, "doctor-physician")
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 404, r.text


async def test_deactivated_agent_chat_404(client, rich, session_factory):
    token = (
        await client.post(
            "/agents/doctor-physician/signup",
            json={"email": EMAIL, "password": PASSWORD},
        )
    ).json()["access_token"]
    await _deactivate(session_factory, "doctor-physician")
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text


async def test_catalog_list_excludes_deactivated(client, rich, session_factory):
    await _deactivate(session_factory, "doctor-physician")
    r = await client.get("/agents")
    assert r.status_code == 200, r.text
    slugs = {a["slug"] for a in r.json()}
    assert "doctor-physician" not in slugs
    assert "corporate-lawyer" in slugs


async def test_deactivated_sub_agent_chat_404(client, rich, session_factory):
    token = (
        await client.post(
            "/agents/doctor-physician/signup",
            json={"email": EMAIL, "password": PASSWORD},
        )
    ).json()["access_token"]
    await _deactivate(session_factory, "doctor-physician-clinical-advisor-agent")
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={
            "message": "hello",
            "sub_agent_slug": "doctor-physician-clinical-advisor-agent",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text


async def test_deactivated_sub_agent_hidden_from_detail(
    client, rich, session_factory
):
    await _deactivate(session_factory, "doctor-physician-clinical-advisor-agent")
    r = await client.get("/agents/doctor-physician")
    assert r.status_code == 200, r.text
    sub_slugs = [s["slug"] for s in r.json()["sub_agents"]]
    assert "doctor-physician-clinical-advisor-agent" not in sub_slugs
    assert "doctor-physician-learning-agent" in sub_slugs
