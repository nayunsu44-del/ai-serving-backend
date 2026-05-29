from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.errors import ProviderAPIError, UnsupportedModelError
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry
from app.schemas import ModelInfo


class FakeProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.last_request: NormalizedChatRequest | None = None

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        self.last_request = request
        return NormalizedChatResponse(
            id="chatcmpl-test",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="mocked response"),
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        self.last_request = request
        yield NormalizedStreamChunk(
            id="chatcmpl-stream",
            model=request.model,
            role="assistant",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-stream",
            model=request.model,
            delta="hello",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-stream",
            model=request.model,
            delta=" world",
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )


class PreStreamFailingProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        raise AssertionError("not used")

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        raise ProviderAPIError(
            provider=self.name,
            upstream_status=500,
            raw_message="raw upstream stream failure",
        )
        yield NormalizedStreamChunk(model=request.model)


class ProviderErrorProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        raise ProviderAPIError(
            provider=self.name,
            upstream_status=500,
            raw_message="raw upstream secret",
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        raise AssertionError("not used")
        yield NormalizedStreamChunk(model=request.model)


class FakeRegistry:
    def __init__(self, provider: FakeProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        if model == "unsupported-model":
            raise UnsupportedModelError(f"Unsupported model: {model}")
        return self.provider

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="gpt-test", owned_by="fake")]


@pytest.mark.asyncio
async def test_chat_completion_non_streaming_uses_mocked_provider(
    app,
    client,
    auth_headers,
):
    provider = FakeProvider()
    app.dependency_overrides[get_provider_registry] = lambda: FakeRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "do not log this"}],
            "temperature": 0.2,
            "max_tokens": 16,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "mocked response",
    }
    assert body["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert provider.last_request is not None
    assert provider.last_request.ignored_fields == []


@pytest.mark.asyncio
async def test_chat_completion_streaming_uses_mocked_provider(
    app,
    client,
    auth_headers,
):
    provider = FakeProvider()
    app.dependency_overrides[get_provider_registry] = lambda: FakeRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "stream"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"object":"chat.completion.chunk"' in response.text
    assert '"content":"hello"' in response.text
    assert '"content":" world"' in response.text
    assert "data: [DONE]" in response.text


@pytest.mark.asyncio
async def test_model_allowlist_rejects_unconfigured_model(client, auth_headers):
    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-not-allowed",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == {
        "message": "Model not found: gpt-not-allowed",
        "type": "invalid_request_error",
        "code": "model_not_found",
    }


@pytest.mark.asyncio
async def test_invalid_utf8_json_returns_validation_error(client, auth_headers):
    response = await client.post(
        "/v1/chat/completions",
        headers={**auth_headers, "Content-Type": "application/json"},
        content=b"{\xff",
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "message": "Invalid request: body: Invalid JSON",
        "type": "invalid_request_error",
        "code": "invalid_request",
    }


@pytest.mark.asyncio
async def test_streaming_preflight_error_returns_json_non_200(
    app,
    client,
    auth_headers,
):
    provider = PreStreamFailingProvider()
    app.dependency_overrides[get_provider_registry] = lambda: FakeRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "stream"}],
            "stream": True,
        },
    )

    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == {
        "message": "Upstream provider error",
        "type": "provider_error",
        "code": "provider_error",
    }


@pytest.mark.asyncio
async def test_provider_error_response_is_sanitized(app, client, auth_headers):
    provider = ProviderErrorProvider()
    app.dependency_overrides[get_provider_registry] = lambda: FakeRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    body = response.json()
    assert response.status_code == 502
    assert body["error"] == {
        "message": "Upstream provider error",
        "type": "provider_error",
        "code": "provider_error",
    }
    assert "raw upstream secret" not in response.text
