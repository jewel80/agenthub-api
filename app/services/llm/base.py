"""LLM provider abstraction.

The chat engine talks ONLY to `LLMProvider`. Swapping providers (Anthropic →
OpenAI → Gemini) means writing one new class + one factory line — the engine
and the rest of the system never change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass(slots=True)
class LLMMessage:
    role: str  # "user" | "assistant"  (system is passed separately)
    content: str


@dataclass(slots=True)
class LLMUsage:
    """Token accounting for one completion (roadmap §4.1/§15)."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def cache_read_tokens(self) -> int:
        return self.cache_read_input_tokens


@dataclass(slots=True)
class CompletionResult:
    """Text + usage for one non-streaming completion."""

    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)


@dataclass(slots=True)
class LLMStreamDelta:
    """A streaming text chunk."""

    text: str


@dataclass(slots=True)
class LLMStreamDone:
    """Terminal streaming event carrying token usage."""

    usage: LLMUsage = field(default_factory=LLMUsage)
    stop_reason: str | None = None


LLMStreamEvent = LLMStreamDelta | LLMStreamDone


class LLMUnavailableError(RuntimeError):
    """The LLM provider is temporarily unavailable (timeout, connection
    failure, rate limit, or 5xx).

    Mapped to HTTP 503 + ``Retry-After`` by the app-level exception handler.
    The message must never contain prompt content or credentials — only the
    error type and status.
    """


class LLMProvider(ABC):
    """Minimal contract every provider implementation satisfies."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        """Return the assistant's text completion for the given turn.

        Implementations raise :class:`LLMUnavailableError` when the upstream
        provider fails transiently; any other exception is a bug.
        """
        ...

    async def complete_with_usage(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> CompletionResult:
        """Text + token usage. Default wraps complete() with empty usage."""
        text = await self.complete(
            system=system, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        return CompletionResult(text=text)

    def stream_complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> AsyncIterator[LLMStreamEvent]:
        """Yield LLMStreamDelta chunks, then one LLMStreamDone.

        Raised before the first delta, this maps to `complete()` failure
        semantics (LLMUnavailableError -> 503). Mid-stream failures also
        raise LLMUnavailableError; the engine handles partial persistence.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not implement streaming"
        )
