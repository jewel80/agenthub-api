"""Deterministic mock provider — for tests and local dev without an API key.

Never used in production. Echoes a marker so tests can assert the engine
plumbing without spending tokens or requiring a key.
"""
from __future__ import annotations

from app.services.llm.base import LLMMessage, LLMProvider


class MockProvider(LLMProvider):
    @property
    def name(self) -> str:
        return "mock"

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        persona_tag = system.strip().splitlines()[0][:60] if system.strip() else "?"
        return (
            f"[mock-llm] persona='{persona_tag}…' "
            f"reply-to='{last_user[:80]}'"
        )
