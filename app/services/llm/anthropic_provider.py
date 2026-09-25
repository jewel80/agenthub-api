"""Anthropic (Claude) provider — the primary LLM implementation.

Supports both direct Anthropic access (ANTHROPIC_API_KEY) and Anthropic-
compatible gateways (ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL, e.g. z.ai).

Transient SDK failures are mapped to the domain error LLMUnavailableError;
the client enforces a request timeout and a single retry. Prompt caching
(roadmap §4.1) marks the stable system prompt and the conversation-prefix
block with `cache_control` breakpoints when LLM_PROMPT_CACHE_ENABLED.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import anthropic
from anthropic import AsyncAnthropic

from app.core.config import settings
from app.services.llm.base import (
    CompletionResult,
    LLMMessage,
    LLMProvider,
    LLMStreamDelta,
    LLMStreamDone,
    LLMUnavailableError,
    LLMUsage,
)

# Transient upstream failures -> LLMUnavailableError (HTTP 503 upstream).
_TRANSIENT_ERRORS = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
    anthropic.APIStatusError,
)


def _build_client() -> AsyncAnthropic:
    """Construct the client from whichever auth style is configured."""
    kwargs: dict = {
        "timeout": settings.LLM_TIMEOUT_SECONDS,
        "max_retries": 1,
    }
    if settings.ANTHROPIC_BASE_URL:
        kwargs["base_url"] = settings.ANTHROPIC_BASE_URL
    if settings.ANTHROPIC_AUTH_TOKEN:
        kwargs["auth_token"] = settings.ANTHROPIC_AUTH_TOKEN
    elif settings.ANTHROPIC_API_KEY:
        kwargs["api_key"] = settings.ANTHROPIC_API_KEY
    return AsyncAnthropic(**kwargs)


def _build_system(system: str) -> str | list[dict]:
    """Cacheable system prompt (roadmap §4.1): one text block with an
    ephemeral breakpoint, so long stable personas are cached provider-side."""
    if not settings.LLM_PROMPT_CACHE_ENABLED:
        return system
    return [
        {
            "type": "text",
            "text": system,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _build_messages(messages: list[LLMMessage]) -> list[dict]:
    """Messages with a breakpoint on the history prefix (all but the newest
    turn), so follow-up requests reuse the cached conversation prefix."""
    payload = [{"role": m.role, "content": m.content} for m in messages]
    if settings.LLM_PROMPT_CACHE_ENABLED and len(payload) >= 2:
        older = payload[-2]
        older["content"] = [
            {
                "type": "text",
                "text": older["content"],
                "cache_control": {"type": "ephemeral"},
            }
        ]
    return payload


def _usage(resp: anthropic.types.Message) -> LLMUsage:
    u = getattr(resp, "usage", None)
    if u is None:
        return LLMUsage()
    return LLMUsage(
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_creation_input_tokens=(
            getattr(u, "cache_creation_input_tokens", 0) or 0
        ),
        cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
    )


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str | None = None) -> None:
        self._client = _build_client()
        self._model = model or settings.ANTHROPIC_MODEL

    @property
    def name(self) -> str:
        return "anthropic"

    async def complete_with_usage(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> CompletionResult:
        try:
            resp = await self._client.messages.create(
                model=self._model,
                system=_build_system(system),
                max_tokens=max_tokens,
                temperature=temperature,
                messages=_build_messages(messages),
            )
        except _TRANSIENT_ERRORS as exc:
            raise self._unavailable(exc) from exc
        text = "".join(
            block.text for block in resp.content if getattr(block, "text", None)
        )
        return CompletionResult(text=text, usage=_usage(resp))

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        result = await self.complete_with_usage(
            system=system, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        return result.text

    async def stream_complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> AsyncIterator[LLMStreamDelta | LLMStreamDone]:
        try:
            async with self._client.messages.stream(
                model=self._model,
                system=_build_system(system),
                max_tokens=max_tokens,
                temperature=temperature,
                messages=_build_messages(messages),
            ) as stream:
                async for text in stream.text_stream:
                    yield LLMStreamDelta(text=text)
                final = await stream.get_final_message()
        except _TRANSIENT_ERRORS as exc:
            raise self._unavailable(exc) from exc
        yield LLMStreamDone(
            usage=_usage(final),
            stop_reason=getattr(final, "stop_reason", None),
        )

    @staticmethod
    def _unavailable(exc: Exception) -> LLMUnavailableError:
        # Error type + HTTP status only — never the prompt or credentials.
        status = getattr(exc, "status_code", None)
        return LLMUnavailableError(
            f"anthropic unavailable: {type(exc).__name__}"
            + (f" (status={status})" if status is not None else "")
        )
