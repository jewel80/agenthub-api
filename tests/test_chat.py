"""Chat endpoint tests (uses the deterministic MockProvider)."""
from __future__ import annotations

EMAIL = "chatter@example.com"
PASSWORD = "supersecret1"


async def _signup(client, slug: str = "doctor-physician") -> str:
    r = await client.post(
        f"/agents/{slug}/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def test_chat_with_main_agent(client, rich):
    token = await _signup(client)
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "I have a persistent cough, what should I do?"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["agent_slug"] == "doctor-physician"
    assert data["sub_agent_slug"] is None
    assert "Doctor" in data["reply"]  # mock echoes persona first-line


async def test_chat_with_sub_agent(client, rich):
    token = await _signup(client)
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={
            "message": "Summarise the latest guideline on hypertension.",
            "sub_agent_slug": "doctor-physician-clinical-advisor-agent",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["sub_agent_slug"] == "doctor-physician-clinical-advisor-agent"
    assert "Clinical Advisor" in data["reply"]


async def test_chat_requires_token(client, rich):
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "hello"},
    )
    assert r.status_code == 401


async def test_cross_agent_chat_forbidden(client, rich):
    """Account under Doctor cannot chat with Lawyer."""
    token = await _signup(client, "doctor-physician")
    r = await client.post(
        "/agents/corporate-lawyer/chat",
        json={"message": "draft a contract"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403, r.text


async def test_chat_unknown_agent_404(client, rich):
    token = await _signup(client)
    r = await client.post(
        "/agents/no-such-agent/chat",
        json={"message": "hi"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


async def test_chat_unknown_sub_agent_404(client, rich):
    token = await _signup(client)
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "hi", "sub_agent_slug": "no-such-sub"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404


async def test_history_returns_persisted_turns(client, rich):
    token = await _signup(client)
    await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "first question"},
        headers={"Authorization": f"Bearer {token}"},
    )
    await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "second question"},
        headers={"Authorization": f"Bearer {token}"},
    )
    r = await client.get(
        "/agents/doctor-physician/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    turns = r.json()
    roles = [t["role"] for t in turns]
    # 2 user turns + 2 assistant replies
    assert roles.count("user") == 2
    assert roles.count("assistant") == 2


async def test_meta_usage_records_chats(client, rich):
    """Observability endpoint reflects agent usage after chatting."""
    token = await _signup(client)
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200

    usage = await client.get("/meta/usage")
    assert usage.status_code == 200
    data = usage.json()
    assert data["total_chats"] >= 1
    assert "doctor-physician" in data["by_target"]


async def test_sub_agent_threads_are_separate(client, rich):
    """Main-agent and sub-agent conversations keep separate histories."""
    token = await _signup(client)
    await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "main thread msg"},
        headers={"Authorization": f"Bearer {token}"},
    )
    await client.post(
        "/agents/doctor-physician/chat",
        json={
            "message": "sub thread msg",
            "sub_agent_slug": "doctor-physician-clinical-advisor-agent",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    main_hist = await client.get(
        "/agents/doctor-physician/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    sub_hist = await client.get(
        "/agents/doctor-physician/history?sub_agent_slug=doctor-physician-clinical-advisor-agent",
        headers={"Authorization": f"Bearer {token}"},
    )
    main_contents = {t["content"] for t in main_hist.json() if t["role"] == "user"}
    sub_contents = {t["content"] for t in sub_hist.json() if t["role"] == "user"}
    assert "main thread msg" in main_contents
    assert "main thread msg" not in sub_contents
    assert "sub thread msg" in sub_contents
