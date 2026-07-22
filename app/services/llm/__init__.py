"""LLM provider factory.

A single cached instance is created from `LLM_PROVIDER`. Add new providers
here (one line in the factory) — nothing else in the system changes.
"""
from __future__ import annotations

from functools import lru_cache

from app.core.config import settings
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMProvider
from app.services.llm.mock_provider import MockProvider


@lru_cache(maxsize=1)
def get_llm_provider() -> LLMProvider:
    name = (settings.LLM_PROVIDER or "mock").lower()

    if name == "mock":
        return MockProvider()

    if name == "anthropic":
        if not settings.ANTHROPIC_API_KEY and not settings.ANTHROPIC_AUTH_TOKEN:
            raise RuntimeError(
                "LLM_PROVIDER=anthropic but no credential is set "
                "(ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN). "
                "Set one, or use LLM_PROVIDER=mock for local dev."
            )
        return AnthropicProvider(model=settings.ANTHROPIC_MODEL)

    raise ValueError(
        f"Unknown LLM_PROVIDER '{name}'. Supported: anthropic, mock."
    )
