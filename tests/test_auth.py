"""Auth flow tests: signup, login, and cross-agent isolation denial."""
from __future__ import annotations

EMAIL = "tester@example.com"
PASSWORD = "supersecret1"


async def test_signup_returns_scoped_token(client, seeded):
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["agent_slug"] == "doctor-physician"
    assert data["agent_profession"] == "Doctor / Physician"
    assert data["access_token"]


async def test_login_under_correct_agent(client, seeded):
    await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 200, r.text
    assert r.json()["agent_slug"] == "doctor-physician"


async def test_cross_agent_login_fails(client, seeded):
    """Account created under Doctor must NOT authenticate under Lawyer."""
    await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    r = await client.post(
        "/agents/corporate-lawyer/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 401, r.text


async def test_same_email_can_register_per_agent(client, seeded):
    """Same email under two different agents = two independent accounts."""
    r1 = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    r2 = await client.post(
        "/agents/corporate-lawyer/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r1.status_code == 201 and r2.status_code == 201

    a = await client.post(
        "/agents/doctor-physician/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    b = await client.post(
        "/agents/corporate-lawyer/login",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["access_token"] != b.json()["access_token"]


async def test_duplicate_signup_same_agent_conflict(client, seeded):
    await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 409, r.text


async def test_wrong_password_fails(client, seeded):
    await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    r = await client.post(
        "/agents/doctor-physician/login",
        json={"email": EMAIL, "password": "wrong-password"},
    )
    assert r.status_code == 401


async def test_password_too_short_rejected(client, seeded):
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": "short"},
    )
    assert r.status_code == 422  # Pydantic min_length validation


async def test_me_requires_token(client, seeded):
    r = await client.get("/me")
    assert r.status_code == 401


async def test_me_returns_scoped_user(client, seeded):
    signup = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    token = signup.json()["access_token"]
    r = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == EMAIL
    assert r.json()["agent_id"] == signup.json()["agent_id"]


async def test_catalog_lists_seeded_agents(client, seeded):
    r = await client.get("/agents")
    assert r.status_code == 200
    slugs = {a["slug"] for a in r.json()}
    assert {"doctor-physician", "corporate-lawyer"}.issubset(slugs)
