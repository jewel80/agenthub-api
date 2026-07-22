"""Anthropic (Claude) provider — the primary LLM implementation.

Supports both direct Anthropic access (ANTHROPIC_API_KEY) and Anthropic-
compatible gateways (ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL, e.g. z.ai).
"""
from __future__ import annotations

from anthropic import AsyncAnthropic

from app.core.config import settings
from app.services.llm.base import LLMMessage, LLMProvider


def _build_client() -> AsyncAnthropic:
    """Construct the client from whichever auth style is configured."""
    kwargs: dict = {}
    if settings.ANTHROPIC_BASE_URL:
        kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
    if settings.ANTHROPIC_AUTH_TOKEN:
        kwargs["auth_token"] = settings.ANTHROPIC_AUTH_TOKEN
    elif settings.ANTHROPIC_API_KEY:
        kwargs["api_key"] = settings.ANTHROPIC_API_KEY
    return AsyncAnthropic(**kwargs)


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str | None = None) -> None:
        self._client = _build_client()
        self._model = model or settings.ANTHROPIC_MODEL

    @property
    def name(self) -> str:
        return "anthropic"

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        resp = await self._client.messages.create(
            model=self._model,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": m.role, "content": m.content} for m in messages],
        )
        # Concatenate all text blocks (handles multi-block responses).
        return "".join(
            block.text for block in resp.content if getattr(block, "text", None)
        )
