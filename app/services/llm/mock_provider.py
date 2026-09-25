"""Deterministic mock provider — for tests and local dev without an API key.

Never used in production. Echoes a marker so tests can assert the engine
plumbing without spending tokens or requiring a key. Streaming splits the
same deterministic reply into word chunks and reports synthetic usage.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from app.services.llm.base import (
    CompletionResult,
    LLMMessage,
    LLMProvider,
    LLMStreamDelta,
    LLMStreamDone,
    LLMUsage,
)


class MockProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "mock"

    def _reply(self, system: str, messages: list[LLMMessage]) -> str:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        persona_tag = system.strip().splitlines()[0][:60] if system.strip() else "?"
        return (
            f"[mock-llm] persona='{persona_tag}…' "
            f"reply-to='{last_user[:80]}'"
        )

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        return self._reply(system, messages)

    async def complete_with_usage(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> CompletionResult:
        text = self._reply(system, messages)
        usage = LLMUsage(
            input_tokens=len(system) // 4 + sum(len(m.content) // 4 for m in messages),
            output_tokens=len(text) // 4,
        )
        return CompletionResult(text=text, usage=usage)

    async def stream_complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> AsyncIterator[LLMStreamDelta | LLMStreamDone]:
        text = self._reply(system, messages)
        for word in text.split(" "):
            yield LLMStreamDelta(text=word + " ")
        yield LLMStreamDone(
            usage=LLMUsage(
                input_tokens=len(system) // 4
                + sum(len(m.content) // 4 for m in messages),
                output_tokens=len(text) // 4,
            ),
            stop_reason="end_turn",
        )
