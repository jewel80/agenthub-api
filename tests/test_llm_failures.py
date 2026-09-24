"""F2 — LLM failure handling tests.

Provider failure must surface as HTTP 503 + Retry-After (never a 500), the
user turn must survive, and no assistant turn may be written.
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from app.core.deps import llm_provider
from app.main import app
from app.services.llm.anthropic_provider import AnthropicProvider
from app.services.llm.base import LLMMessage, LLMProvider, LLMUnavailableError
from app.services.llm.mock_provider import MockProvider

EMAIL = "failsafe@example.com"
PASSWORD = "supersecret1"


class UnavailableProvider(LLMProvider):
    """Stub that always fails like an unreachable LLM."""

    @property
    def name(self) -> str:
        return "unavailable"

    async def complete(
        self,
        *,
        system: str,
        messages: list[LLMMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> str:
        raise LLMUnavailableError("anthropic unavailable: APITimeoutError")


async def _signup(client) -> str:
    r = await client.post(
        "/agents/doctor-physician/signup",
        json={"email": EMAIL, "password": PASSWORD},
    )
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


async def test_provider_failure_returns_503_with_retry_after(client, rich):
    app.dependency_overrides[llm_provider] = lambda: UnavailableProvider()
    try:
        token = await _signup(client)
        r = await client.post(
            "/agents/doctor-physician/chat",
            json={"message": "hello?"},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides[llm_provider] = lambda: MockProvider()

    assert r.status_code == 503, r.text
    assert r.headers.get("Retry-After") == "30"
    assert r.json()["detail"] == (
        "The assistant is temporarily unavailable. Please try again."
    )


async def test_user_turn_survives_and_no_assistant_row(client, rich):
    app.dependency_overrides[llm_provider] = lambda: UnavailableProvider()
    try:
        token = await _signup(client)
        await client.post(
            "/agents/doctor-physician/chat",
            json={"message": "please answer"},
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        app.dependency_overrides[llm_provider] = lambda: MockProvider()

    r = await client.get(
        "/agents/doctor-physician/history",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    turns = r.json()
    assert len(turns) == 1
    assert turns[0]["role"] == "user"
    assert turns[0]["content"] == "please answer"


def _sdk_error(exc_type: type[Exception], status: int | None) -> Exception:
    """Build a real anthropic SDK exception without any network access."""
    request = httpx.Request("POST", "https://api.test.invalid/v1/messages")
    if exc_type is APITimeoutError:
        return APITimeoutError(request=request)
    if exc_type is APIConnectionError:
        return APIConnectionError(message="conn", request=request)
    response = httpx.Response(status or 500, request=request)
    return exc_type("boom", response=response, body=None)


class _FailingMessages:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def create(self, **_kwargs):
        raise self._exc


@pytest.mark.parametrize(
    ("exc_type", "status"),
    [
        (APITimeoutError, None),
        (APIConnectionError, None),
        (APIStatusError, 500),
        (APIStatusError, 529),
        (RateLimitError, 429),
    ],
)
async def test_anthropic_provider_maps_transient_errors(
    monkeypatch, exc_type, status
):
    failing_client = SimpleNamespace(
        messages=_FailingMessages(_sdk_error(exc_type, status))
    )
    monkeypatch.setattr(
        "app.services.llm.anthropic_provider._build_client",
        lambda: failing_client,
    )
    provider = AnthropicProvider(model="test-model")

    with pytest.raises(LLMUnavailableError) as excinfo:
        await provider.complete(system="s", messages=[LLMMessage("user", "hi")])

    # sanitized message: error type (+ status), never prompt content
    assert exc_type.__name__ in str(excinfo.value)
    if status is not None:
        assert f"status={status}" in str(excinfo.value)
