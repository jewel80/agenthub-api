"""F5 — signup race (IntegrityError path) and F6 — readiness endpoint."""
from __future__ import annotations

from app.core import db as core_db
from app.core.security import hash_password
from app.models.agent import Agent
from app.models.user import User
from app.services import auth_service

EMAIL = "racer@example.com"
PASSWORD = "supersecret1"


async def _create_account_directly(session_factory, rich) -> User:
    """Create the account behind the API's back, as a concurrent winner."""
    async with session_factory() as s:
        doctor = (
            await s.execute(
                Agent.__table__.select().where(Agent.slug == "doctor-physician")
            )
        ).first()
        user = User(
            email=EMAIL,
            password_hash=hash_password(PASSWORD),
            agent_id=doctor.id,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


def _patch_lookup_to_miss_once(monkeypatch):
    """Simulate the race: signup's first SELECT misses the existing row."""
    from app.repositories import user_repo

    real = user_repo.get_user_by_email_and_agent
    state = {"missed": False}

    async def miss_once(db, email, agent_id):
        if state["missed"]:
            return await real(db, email, agent_id)
        state["missed"] = True
        return None

    monkeypatch.setattr(
        auth_service.user_repo, "get_user_by_email_and_agent", miss_once
    )


async def test_signup_race_correct_password_returns_token(
    client, rich, session_factory, monkeypatch
):
    await _create_account_directly(session_factory, rich)
    _patch_lookup_to_miss_once(monkeypatch)

    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 200, r.text  # idempotent login, never a 500
    assert r.json()["access_token"]
    assert r.json()["agent_slug"] == "doctor-physician"


async def test_signup_race_wrong_password_conflicts(
    client, rich, session_factory, monkeypatch
):
    await _create_account_directly(session_factory, rich)
    _patch_lookup_to_miss_once(monkeypatch)

    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": "wrong-password"},
    )
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# F6 — readiness
# ---------------------------------------------------------------------------


async def test_ready_200_when_db_reachable(client):
    r = await client.get("/health/ready")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ready"}


class _BrokenEngine:
    """Engine stub whose connections always fail (simulates DB down)."""

    def connect(self):
        raise ConnectionError("database unreachable")


async def test_ready_503_and_health_200_when_db_down(client, monkeypatch):
    monkeypatch.setattr(core_db, "engine", _BrokenEngine())
    ready = await client.get("/health/ready")
    live = await client.get("/health")
    assert ready.status_code == 503, ready.text
    assert live.status_code == 200  # liveness stays cheap and green
    assert live.json()["status"] == "ok"
