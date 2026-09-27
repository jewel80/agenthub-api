"""THE generic chat engine — one code path for every agent.

This module is the proof that "agents are config, not code":
  1. resolve agent_slug (+optional sub_agent_slug) to a config row + system prompt
  2. enforce the user's auth scope (tenant isolation)
  3. merge conversation history (scoped to the target sub-agent thread)
  4. call the LLM through the LLMProvider interface (non-streaming or SSE)
  5. persist the turn and return the reply

There is NO branching on which agent it is. Adding agent #101 never touches
this file — it's just a new agents row whose system_prompt drives behaviour.

Persistence decisions:
- Non-streaming: the user turn is committed *before* the LLM call and
  survives it; no assistant turn is written when the provider fails.
- Streaming (roadmap §3.3): same for the user turn; during the stream no DB
  connection is held. On completion the assistant turn is written with token
  usage; on client disconnect the partial reply is saved with
  status="interrupted"; on provider error with status="failed". A failure
  with zero accumulated text writes nothing (an empty reply is not a reply).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import mark_read_your_writes
from app.core.deps import require_agent_scope
from app.models.agent import Agent
from app.models.user import User
from app.repositories import agent_repo, message_repo
from app.services.agent_service import get_active_main_agent_or_404
from app.services.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMStreamDelta,
    LLMStreamDone,
    LLMUnavailableError,
    LLMUsage,
)
from app.services.observability import get_usage_tracker
from app.services.rate_limiter import get_token_quota, llm_concurrency_cap

logger = logging.getLogger("agenthub.chat")

# Conversation window retained per request (keeps token cost bounded).
HISTORY_WINDOW = 20
# SSE heartbeat cadence so proxies keep the connection open (roadmap §3.2).
HEARTBEAT_SECONDS = 15.0


def conversation_id_for(
    user_id: uuid.UUID, agent_id: uuid.UUID, sub_agent_id: uuid.UUID | None
) -> str:
    """Stable id for a (user, main agent, sub-agent) thread.

    Deterministic (uuid5) so repeat chats map to the same conversation. The
    first-class `conversations` entity (roadmap §6) will supersede this.
    """
    key = f"agenthub:thread:{user_id}:{agent_id}:{sub_agent_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


async def resolve_target(
    db: AsyncSession,
    user: User,
    agent_slug: str,
    sub_agent_slug: str | None = None,
) -> tuple[Agent, Agent]:
    """Resolve (main_agent, target_agent) and enforce the user's scope.

    `target` is the sub-agent if `sub_agent_slug` is given, else the main agent.
    Raises 403 if the user's token agent_id doesn't own this resource.
    """
    # Shared helper: unknown and deactivated agents both 404 (fix-doc F4).
    main = await get_active_main_agent_or_404(db, agent_slug)

    require_agent_scope(user, main.id)  # tenant isolation

    target = main
    if sub_agent_slug:
        sub = await agent_repo.get_sub_agent_by_slug(db, main.id, sub_agent_slug)
        if sub is None or not sub.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Sub-agent not found.")
        target = sub
    return main, target


def _require_token_quota(user_id: uuid.UUID) -> None:
    """429 when the user exhausted today's token quota (checked async by the
    caller via TokenQuotaLimiter.check; this only formats the error)."""
    raise HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "Daily token quota exceeded. The quota resets at midnight UTC.",
    )


@dataclass(slots=True)
class TurnResult:
    reply: str
    usage: LLMUsage


async def run_turn(
    db: AsyncSession,
    user: User,
    *,
    main: Agent,
    target: Agent,
    message: str,
    provider: LLMProvider,
) -> TurnResult:
    """Run one (non-streaming) chat turn against a resolved (main, target).

    Transaction boundaries: the user turn is flushed *and committed* before
    the LLM call (so its `seq` precedes the reply's, and no DB transaction is
    held open across the slow provider call). The assistant turn is committed
    after a successful completion only.
    """
    sub_id = target.id if target.parent_id is not None else None

    if not await get_token_quota().check(str(user.id)):
        _require_token_quota(user.id)

    # 1. persist the user's turn (flushed first, so its `seq` is always lower
    #    than the assistant turn that follows) and commit: the turn is the
    #    user's data and must survive an LLM outage.
    await message_repo.add_message(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        role="user",
        content=message,
    )
    await db.commit()
    await mark_read_your_writes(user.id)

    # 2. load scoped history (includes the turn just persisted); pair_safe
    #    drops a leading assistant turn so the LLM context opens with `user`
    history = await message_repo.get_recent_history(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        limit=HISTORY_WINDOW,
        pair_safe=True,
    )
    llm_messages = [
        LLMMessage(role=h.role, content=h.content)
        for h in history
        if h.role in ("user", "assistant")
    ]

    # 3. call the provider with the target's system prompt (the persona);
    #    log the request WITHOUT message content (privacy + log hygiene)
    logger.info(
        "chat user=%s main=%s target=%s provider=%s history_len=%d",
        user.id, main.slug, target.slug, provider.name, len(llm_messages),
    )
    cap = llm_concurrency_cap()
    try:
        if cap is None:
            result = await _complete(provider, target, llm_messages)
        else:
            async with cap:
                result = await _complete(provider, target, llm_messages)
    except LLMUnavailableError as exc:
        # Type/status only — never the prompt or credentials. The user turn
        # stays committed; no assistant row is written.
        logger.warning(
            "llm unavailable user=%s target=%s provider=%s error=%s",
            user.id, target.slug, provider.name, exc,
        )
        raise

    # 4. persist the assistant's reply and account the tokens
    await message_repo.add_message(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        role="assistant",
        content=result.text,
    )
    await db.commit()
    await mark_read_your_writes(user.id)
    await get_token_quota().record(
        str(user.id),
        result.usage.input_tokens + result.usage.output_tokens,
    )

    # basic observability: which agent/sub-agent got used
    get_usage_tracker().record(
        main.slug, target.slug if target.id != main.id else None
    )
    return TurnResult(reply=result.text, usage=result.usage)


@asynccontextmanager
async def _optional_semaphore(sem: asyncio.Semaphore | None):
    """No-op when `sem` is None, else hold it for the wrapped block."""
    if sem is None:
        yield
    else:
        async with sem:
            yield


async def _complete(
    provider: LLMProvider, target: Agent, messages: list[LLMMessage]
):
    return await provider.complete_with_usage(
        system=target.system_prompt,
        messages=messages,
        max_tokens=1024,
        temperature=0.7,
    )


# ---------------------------------------------------------------------------
# Streaming (roadmap §3)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StreamPrepared:
    """Everything the stream needs after the request DB session is closed."""

    user: User
    main: Agent
    target: Agent
    sub_agent_id: uuid.UUID | None
    message_id: str          # pre-generated assistant message id
    conversation_id: str
    system: str
    llm_messages: list[LLMMessage]


@dataclass(slots=True)
class StartEvent:
    message_id: str
    conversation_id: str


@dataclass(slots=True)
class DeltaEvent:
    text: str


@dataclass(slots=True)
class DoneEvent:
    usage: LLMUsage
    stop_reason: str | None


@dataclass(slots=True)
class ErrorEvent:
    code: str
    message: str


class Heartbeat:
    """Sentinel: emit `: ping` and keep waiting."""


StreamEvent = StartEvent | DeltaEvent | DoneEvent | ErrorEvent | Heartbeat


async def prepare_stream_turn(
    db: AsyncSession,
    user: User,
    *,
    main: Agent,
    target: Agent,
    message: str,
) -> StreamPrepared:
    """Persist + commit the user turn and snapshot the LLM context.

    Runs entirely inside the request's DB session, which the router releases
    BEFORE streaming starts — no DB connection is held during the stream.
    """
    sub_id = target.id if target.parent_id is not None else None

    if not await get_token_quota().check(str(user.id)):
        _require_token_quota(user.id)

    await message_repo.add_message(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        role="user",
        content=message,
    )
    await db.commit()
    await mark_read_your_writes(user.id)

    history = await message_repo.get_recent_history(
        db,
        user_id=user.id,
        agent_id=main.id,
        sub_agent_id=sub_id,
        limit=HISTORY_WINDOW,
        pair_safe=True,
    )
    return StreamPrepared(
        user=user,
        main=main,
        target=target,
        sub_agent_id=sub_id,
        message_id=str(uuid.uuid4()),
        conversation_id=conversation_id_for(user.id, main.id, sub_id),
        system=target.system_prompt,
        llm_messages=[
            LLMMessage(role=h.role, content=h.content)
            for h in history
            if h.role in ("user", "assistant")
        ],
    )


async def stream_turn(
    prepared: StreamPrepared,
    provider: LLMProvider,
    *,
    session_factory: async_sessionmaker,
    max_seconds: float | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream one chat turn as engine events (SSE encoding is the router's).

    Persistence rules (roadmap §3.3): the assistant turn is written in a
    short dedicated session when the stream finishes — status complete,
    interrupted (client disconnect), or failed (provider error / timeout).
    Zero-text failures persist nothing.
    """
    user, target = prepared.user, prepared.target
    deadline = time.monotonic() + (
        max_seconds if max_seconds is not None else float("inf")
    )
    text_parts: list[str] = []
    final_usage: LLMUsage | None = None
    stop_reason: str | None = None
    final_status = "complete"

    yield StartEvent(prepared.message_id, prepared.conversation_id)
    logger.info(
        "stream start user=%s target=%s provider=%s message_id=%s",
        user.id, target.slug, provider.name, prepared.message_id,
    )

    gen = provider.stream_complete(
        system=prepared.system,
        messages=prepared.llm_messages,
        max_tokens=1024,
        temperature=0.7,
    )
    try:
        # Global LLM concurrency cap (roadmap §5) applies to streams too —
        # held for the whole upstream connection, not just the setup call.
        async with _optional_semaphore(llm_concurrency_cap()):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    event = await asyncio.wait_for(
                        gen.__anext__(), timeout=min(HEARTBEAT_SECONDS, remaining)
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    yield Heartbeat()
                    continue
                if isinstance(event, LLMStreamDelta):
                    text_parts.append(event.text)
                    yield DeltaEvent(event.text)
                elif isinstance(event, LLMStreamDone):
                    final_usage = event.usage
                    stop_reason = event.stop_reason
    except LLMUnavailableError as exc:
        logger.warning(
            "stream failed user=%s target=%s provider=%s error=%s",
            user.id, target.slug, provider.name, exc,
        )
        final_status = "failed"
        yield ErrorEvent(
            "llm_unavailable", "The assistant is temporarily unavailable."
        )
    except TimeoutError:
        logger.warning(
            "stream timeout user=%s target=%s message_id=%s",
            user.id, target.slug, prepared.message_id,
        )
        final_status = "failed"
        yield ErrorEvent(
            "stream_timeout", "The reply took too long and was cut off."
        )
    except (asyncio.CancelledError, GeneratorExit):
        # client disconnect: keep the partial reply, mark interrupted.
        # (aclose() raises GeneratorExit; task cancellation raises
        # CancelledError — both mean the consumer went away.)
        final_status = "interrupted"
        raise
    else:
        yield DoneEvent(final_usage or LLMUsage(), stop_reason)
    finally:
        text = "".join(text_parts)
        if text or final_status == "complete":
            coro = _persist_streamed_turn(
                session_factory, prepared, text, final_status, final_usage
            )
            try:
                await asyncio.shield(coro)
            except Exception:
                logger.exception(
                    "could not persist streamed turn message_id=%s",
                    prepared.message_id,
                )


async def _persist_streamed_turn(
    session_factory: async_sessionmaker,
    prepared: StreamPrepared,
    text: str,
    final_status: str,
    usage: LLMUsage | None,
) -> None:
    """Write the assistant turn (short session; never holds the stream)."""
    from app.models.message import Message  # local import avoids cycles

    async with session_factory() as session:
        msg = Message(
            id=uuid.UUID(prepared.message_id),
            user_id=prepared.user.id,
            agent_id=prepared.main.id,
            sub_agent_id=prepared.sub_agent_id,
            role="assistant",
            content=text,
            status=final_status,
        )
        session.add(msg)
        await session.commit()
    await mark_read_your_writes(prepared.user.id)

    if usage is not None:
        await get_token_quota().record(
            str(prepared.user.id),
            usage.input_tokens + usage.output_tokens,
        )
    get_usage_tracker().record(
        prepared.main.slug,
        prepared.target.slug if prepared.target.id != prepared.main.id else None,
    )
