"""LLM provider abstraction.

The chat engine talks ONLY to `LLMProvider`. Swapping providers (Anthropic →
OpenAI → Gemini) means writing one new class + one factory line — the engine
and the rest of the system never change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class LLMMessage:
    role: str  # "user" | "assistant"  (system is passed separately)
    content: str


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
