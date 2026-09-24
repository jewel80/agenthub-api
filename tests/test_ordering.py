"""F1 — conversation ordering tests.

`seq` (identity) is the authoritative turn order; user and assistant turns
from one transaction must never scramble. The LLM context must open with a
user message and end with the current user message.
"""
from __future__ import annotations

import itertools
import uuid

from app.core.deps import llm_provider
from app.main import app
from app.models.message import Message
from app.repositories import message_repo
from app.services.llm.base import LLMMessage, LLMProvider

EMAIL = "order@example.com"
PASSWORD = "supersecret1"


class CapturingProvider(LLMProvider):
    """Mock provider that records the message list it was called with."""

    def __init__(self) -> None:
        self.calls: list[list[LLMMessage]] = []

    @property
    def name(self) -> str:
        return "capturing"

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        self.calls.append(list(messages))
        return f"reply #{len(self.calls)}"


async def _signup(client, slug: str = "doctor-physician") -> str:
    r = await client.post(
        f"/agents/{slug}/signup", json={"email": EMAIL, "password": PASSWORD}
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def test_history_strictly_alternating_in_sent_order(client, rich):
    """5 chats -> history is exactly u,a,u,a,u,a,u,a,u,a with our contents."""
    token = await _signup(client)
    sent = [f"message number {i}" for i in range(1, 6)]
    for msg in sent:
        r = await client.post(
            "/agents/doctor-physician/chat",
            json={"message": msg},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text

    r = await client.get(
        "/agents/doctor-physician/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    turns = r.json()
    assert len(turns) == 10
    roles = [t["role"] for t in turns]
    assert roles == ["user", "assistant"] * 5
    # user turns come back in exactly the order they were sent
    assert [t["content"] for t in turns if t["role"] == "user"] == sent


async def test_llm_context_starts_with_user_ends_with_current(client, rich):
    """The provider-visible context opens with `user` and ends with the
    current user message."""
    provider = CapturingProvider()
    app.dependency_overrides[llm_provider] = lambda: provider
    try:
        token = await _signup(client)
        for msg in ("first question", "second question", "third question"):
            r = await client.post(
                "/agents/doctor-physician/chat",
                json={"message": msg},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200, r.text
    finally:
        # restore the suite default override (mock provider)
        from app.services.llm.mock_provider import MockProvider

        app.dependency_overrides[llm_provider] = lambda: MockProvider()

    assert len(provider.calls) == 3
    last_context = provider.calls[-1]
    assert last_context[0].role == "user", "context must start with a user turn"
    assert last_context[-1].role == "user"
    assert last_context[-1].content == "third question"
    # alternating roles throughout
    for prev, cur in itertools.pairwise(last_context):
        assert prev.role != cur.role


async def test_history_window_is_pair_safe(session_factory, rich):
    """A window that would start with an assistant turn drops it."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.user import User

    async with session_factory() as s:
        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(
            email="pairsafe@example.com",
            password_hash="x",
            agent_id=doctor.id,
        )
        s.add(user)
        await s.flush()

        # 3 complete pairs + a trailing orphan assistant (e.g. a failed turn
        # from legacy data) = 7 turns. A limit-6 window therefore starts at
        # a1's predecessor: [a0, q1, a1, q2, a2, orphan].
        for i in range(3):
            s.add(
                Message(
                    user_id=user.id,
                    agent_id=doctor.id,
                    sub_agent_id=None,
                    role="user",
                    content=f"q{i}",
                )
            )
            s.add(
                Message(
                    user_id=user.id,
                    agent_id=doctor.id,
                    sub_agent_id=None,
                    role="assistant",
                    content=f"a{i}",
                )
            )
        s.add(
            Message(
                user_id=user.id,
                agent_id=doctor.id,
                sub_agent_id=None,
                role="assistant",
                content="orphan",
            )
        )
        await s.commit()

        plain = await message_repo.get_recent_history(
            s, user_id=user.id, agent_id=doctor.id, sub_agent_id=None, limit=6
        )
        safe = await message_repo.get_recent_history(
            s,
            user_id=user.id,
            agent_id=doctor.id,
            sub_agent_id=None,
            limit=6,
            pair_safe=True,
        )
        assert plain[0].role == "assistant"  # window cut between q0 and a0
        assert safe[0].role == "user"  # leading assistant dropped
        assert len(safe) == len(plain) - 1


async def test_sub_agent_threads_ordered_separately(client, rich):
    """Sub-agent turns get their own seq stream *within their thread*: main
    and sub histories each stay correctly ordered and isolated."""
    token = await _signup(client)
    headers = {"Authorization": f"Bearer {token}"}
    for msg in ("main one", "main two"):
        await client.post(
            "/agents/doctor-physician/chat",
            json={"message": msg},
            headers=headers,
        )
    for msg in ("sub one",):
        await client.post(
            "/agents/doctor-physician/chat",
            json={
                "message": msg,
                "sub_agent_slug": "doctor-physician-clinical-advisor-agent",
            },
            headers=headers,
        )

    main_hist = await client.get(
        "/agents/doctor-physician/history", headers=headers
    )
    sub_hist = await client.get(
        "/agents/doctor-physician/history"
        "?sub_agent_slug=doctor-physician-clinical-advisor-agent",
        headers=headers,
    )
    assert [t["content"] for t in main_hist.json() if t["role"] == "user"] == [
        "main one",
        "main two",
    ]
    assert [t["content"] for t in sub_hist.json() if t["role"] == "user"] == [
        "sub one"
    ]


async def test_history_limit_validation(client, rich):
    token = await _signup(client)
    headers = {"Authorization": f"Bearer {token}"}
    await client.post(
        "/agents/doctor-physician/chat",
        json={"message": "hello"},
        headers=headers,
    )
    assert (
        await client.get(
            "/agents/doctor-physician/history?limit=0", headers=headers
        )
    ).status_code == 422
    assert (
        await client.get(
            "/agents/doctor-physician/history?limit=101", headers=headers
        )
    ).status_code == 422
    r = await client.get(
        "/agents/doctor-physician/history?limit=1", headers=headers
    )
    assert r.status_code == 200
    assert len(r.json()) == 1  # the newest turn (assistant reply)
    assert r.json()[0]["role"] == "assistant"


async def test_seq_assigns_monotonic_values(session_factory, rich):
    """Raw check: consecutive inserts get strictly increasing seq."""
    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.user import User

    async with session_factory() as s:
        doctor = (
            await s.execute(select(Agent).where(Agent.slug == "doctor-physician"))
        ).scalars().one()
        user = User(
            email=f"seq-{uuid.uuid4().hex[:6]}@x.com",
            password_hash="x",
            agent_id=doctor.id,
        )
        s.add(user)
        await s.flush()
        seqs = []
        for role, content in (("user", "u"), ("assistant", "a"), ("user", "u2")):
            m = await message_repo.add_message(
                s,
                user_id=user.id,
                agent_id=doctor.id,
                sub_agent_id=None,
                role=role,
                content=content,
            )
            seqs.append(m.seq)
        assert seqs == sorted(seqs) and len(set(seqs)) == 3
