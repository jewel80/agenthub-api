"""Roadmap §5 — daily token quota + login brute-force lockout tests."""
from __future__ import annotations

from app.services.rate_limiter import TokenQuotaLimiter

EMAIL = "quota@example.com"
PASSWORD = "supersecret1"


async def _signup(client, email: str = EMAIL) -> str:
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": email, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def test_chat_blocked_when_token_quota_exhausted(client, rich, monkeypatch):
    from app.services import chat_engine

    token = await _signup(client, email="quota1@example.com")
    user_id = (
        await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    ).json()["id"]

    quota = TokenQuotaLimiter(daily_quota=100)
    await quota.record(user_id, 100)  # exactly at quota
    monkeypatch.setattr(chat_engine, "get_token_quota", lambda: quota)

    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 429, r.text
    assert "quota" in r.json()["detail"].lower()


async def test_stream_blocked_when_token_quota_exhausted(
    client, rich, session_factory, monkeypatch
):
    from app.core.deps import stream_db_factory
    from app.main import app

    token = await _signup(client, email="quota2@example.com")
    user_id = (
        await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    ).json()["id"]

    quota = TokenQuotaLimiter(daily_quota=10)
    await quota.record(user_id, 10)
    from app.api.routers import chat as chat_router

    monkeypatch.setattr(chat_router, "get_token_quota", lambda: quota)
    app.dependency_overrides[stream_db_factory] = lambda: session_factory

    r = await client.post(
        "/v1/agents/doctor-physician/chat/stream",
        json={"message": "hello"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 429, r.text
    assert "quota" in r.json()["detail"].lower()


async def test_token_quota_records_usage_after_chat(client, rich, monkeypatch):
    from app.services import chat_engine

    quota = TokenQuotaLimiter(daily_quota=1_000_000)
    monkeypatch.setattr(chat_engine, "get_token_quota", lambda: quota)
    token = await _signup(client, email="quota3@example.com")
    r = await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "count my tokens"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    user_id = (await client.get(
        "/me", headers={"Authorization": f"Bearer {token}"}
    )).json()["id"]
    assert quota.used(user_id) > 0  # mock provider reports synthetic usage


async def test_login_lockout_after_repeated_failures(client, seeded):
    """5 wrong passwords -> 429 lockout, even with the correct password."""
    await _signup(client, email="locked@example.com")
    for i in range(5):
        r = await client.post(
            "/agents/doctor-physician/login",
            json={"email": "locked@example.com", "password": f"wrong-pass-{i}"},
        )
        assert r.status_code == 401, r.text
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": "locked@example.com", "password": "supersecret1"},
    )
    assert r.status_code == 429, r.text
    assert r.headers.get("Retry-After")
    assert 1 <= int(r.headers["Retry-After"]) <= 30  # first lock step


async def test_login_lockout_is_per_account(client, seeded):
    await _signup(client, email="lock-a@example.com")
    await _signup(client, email="lock-b@example.com")
    for _ in range(5):
        await client.post(
            "/agents/doctor-physician/login",
            json={"email": "lock-a@example.com", "password": "wrong-pass"},
        )
    # a different account is unaffected
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": "lock-b@example.com", "password": "supersecret1"},
    )
    assert r.status_code == 200, r.text


async def test_login_rate_limit_is_stricter_than_lockout(client, seeded, monkeypatch):
    """A tight LOGIN_RATE_LIMIT_PER_MIN blocks attempts before the failure
    count would ever reach LOGIN_MAX_FAILURES (roadmap §5)."""
    from app.services import rate_limiter as rate_limiter_mod

    monkeypatch.setattr(rate_limiter_mod.settings, "LOGIN_RATE_LIMIT_PER_MIN", 2)
    rate_limiter_mod._login_limiter = None
    await _signup(client, email="rl@example.com")

    responses = [
        await client.post(
            "/agents/doctor-physician/login",
            json={"email": "rl@example.com", "password": "wrong-pass"},
        )
        for _ in range(3)
    ]
    assert [r.status_code for r in responses[:2]] == [401, 401]
    assert responses[2].status_code == 429
    assert responses[2].headers.get("Retry-After")


async def test_login_success_clears_failure_history(client, seeded):
    await _signup(client, email="lock-c@example.com")
    for _ in range(4):  # under the threshold
        await client.post(
            "/agents/doctor-physician/login",
            json={"email": "lock-c@example.com", "password": "wrong-pass"},
        )
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": "lock-c@example.com", "password": "supersecret1"},
    )
    assert r.status_code == 200
    # failure count was reset: four more misses do not lock
    for _ in range(4):
        await client.post(
            "/agents/doctor-physician/login",
            json={"email": "lock-c@example.com", "password": "wrong-pass"},
        )
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": "lock-c@example.com", "password": "supersecret1"},
    )
    assert r.status_code == 200, r.text
