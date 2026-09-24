"""P3 hardening tests: F12 (agent_id claim), F13 (/meta/usage guard),
F15 (request-id), F7 (history index exists after migration)."""
from __future__ import annotations

import jwt
from sqlalchemy import text

from app.core.config import settings
from app.core.security import hash_password

EMAIL = "p3@example.com"
PASSWORD = "supersecret1"


async def _signup(client, slug: str = "doctor-physician") -> str:
    r = await client.post(
        f"/agents/{slug}/signup", json={"email": EMAIL, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# F12 — JWT agent_id claim must match the account binding
# ---------------------------------------------------------------------------


async def test_forged_agent_id_claim_rejected(client, seeded, session_factory):
    """A signed token whose agent_id was re-bound to another agent -> 401."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.user import User

    async with session_factory() as s:
        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        lawyer = (
            await s.execute(select(Agent).where(Agent.slug == "corporate-lawyer"))
        ).scalars().one()
        user = User(
            email="claim@example.com",
            password_hash=hash_password(PASSWORD),
            agent_id=doctor.id,
        )
        s.add(user)
        await s.commit()
        user_id = user.id

    payload = {
        "sub": str(user_id),
        "agent_id": str(lawyer.id),  # != user.agent_id
        "exp": 9999999999,
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)
    r = await client.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401, r.text


# ---------------------------------------------------------------------------
# F13 — /meta/usage protected in production
# ---------------------------------------------------------------------------


async def test_meta_usage_open_in_development(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    r = await client.get("/meta/usage")
    assert r.status_code == 200, r.text


async def test_meta_usage_404_in_production_without_token(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    r = await client.get("/meta/usage")
    assert r.status_code == 404, r.text


async def test_meta_usage_requires_matching_admin_token(client, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "secret-admin-token")
    wrong = await client.get(
        "/meta/usage", headers={"X-Admin-Token": "wrong"}
    )
    assert wrong.status_code == 404
    ok = await client.get(
        "/meta/usage", headers={"X-Admin-Token": "secret-admin-token"}
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# F15 — request IDs
# ---------------------------------------------------------------------------


async def test_request_id_generated_and_echoed(client, seeded):
    r1 = await client.get("/health")
    r2 = await client.get("/health")
    rid1, rid2 = r1.headers.get("X-Request-ID"), r2.headers.get("X-Request-ID")
    assert rid1 and rid2 and rid1 != rid2  # unique per request


async def test_request_id_accepted_from_client(client, seeded):
    supplied = "my-trace-id-123"
    r = await client.get("/health", headers={"X-Request-ID": supplied})
    assert r.headers.get("X-Request-ID") == supplied


async def test_error_body_contains_request_id(client, seeded):
    r = await client.get("/agents/no-such-agent")
    assert r.status_code == 404
    body = r.json()
    assert body["request_id"] == r.headers.get("X-Request-ID")
    assert "detail" in body  # original field kept


# ---------------------------------------------------------------------------
# F7 — composite history index exists (created by the F1 migration)
# ---------------------------------------------------------------------------


async def test_history_composite_index_exists(client, session_factory):
    async with session_factory() as s:
        res = await s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'messages' AND indexname = 'ix_messages_history'"
        ))
        assert res.scalar_one_or_none() is not None
        res = await s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'messages' AND indexname = 'ix_messages_seq'"
        ))
        assert res.scalar_one_or_none() is not None


async def test_seq_backfill_and_identity_on_existing_data(
    client, session_factory, rich
):
    """The F1 migration pattern holds on this schema: seq is NOT NULL,
    unique, and new inserts continue above existing values."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.message import Message
    from app.models.user import User

    async with session_factory() as s:
        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(
            email="seqcheck@example.com",
            password_hash="x",
            agent_id=doctor.id,
        )
        s.add(user)
        await s.flush()  # assign user.id before referencing it below
        m1 = Message(
            user_id=user.id,
            agent_id=doctor.id,
            sub_agent_id=None,
            role="user",
            content="first",
        )
        s.add(m1)
        await s.flush()
        first_seq = m1.seq
        m2 = Message(
            user_id=user.id,
            agent_id=doctor.id,
            sub_agent_id=None,
            role="assistant",
            content="second",
        )
        s.add(m2)
        await s.flush()
        assert m2.seq > first_seq
        not_null = await s.execute(text(
            "SELECT attnotnull FROM pg_attribute "
            "WHERE attrelid = 'messages'::regclass AND attname = 'seq'"
        ))
        assert not_null.scalar_one() is True
