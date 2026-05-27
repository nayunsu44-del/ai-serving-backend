from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedStreamChunk,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


AUTH_HEADERS = {"Authorization": "Bearer test-key"}


class FakeRegistry:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


class BlockingStreamProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        raise AssertionError("not used")

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        try:
            self.started.set()
            yield NormalizedStreamChunk(
                id="chatcmpl-blocking",
                model=request.model,
                role="assistant",
            )
            await self.release.wait()
            yield NormalizedStreamChunk(
                id="chatcmpl-blocking",
                model=request.model,
                delta="done",
                finish_reason="stop",
            )
        finally:
            self.closed = True


class SlowStreamProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.closed = False

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        raise AssertionError("not used")

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        try:
            yield NormalizedStreamChunk(
                id="chatcmpl-slow",
                model=request.model,
                role="assistant",
            )
            await asyncio.sleep(2)
            yield NormalizedStreamChunk(
                id="chatcmpl-slow",
                model=request.model,
                delta="too late",
            )
        finally:
            self.closed = True


def _settings(**overrides) -> Settings:
    defaults = {
        "api_keys": ["test-key"],
        "rate_limit_rpm": 1000,
        "openai_models": ["gpt-test"],
        "anthropic_models": [],
    }
    defaults.update(overrides)
    return Settings(**defaults)


async def _post_chat(client: AsyncClient, payload: dict) -> object:
    return await client.post(
        "/v1/chat/completions",
        headers=AUTH_HEADERS,
        json=payload,
    )


@pytest.mark.asyncio
async def test_oversize_body_returns_413():
    app = create_app(_settings(max_request_bytes=32))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/chat/completions",
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            content=b'{"model":"gpt-test","messages":[{"role":"user","content":"too large"}]}',
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


@pytest.mark.asyncio
async def test_too_many_messages_returns_422():
    app = create_app(_settings(max_messages=1))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await _post_chat(
            client,
            {
                "model": "gpt-test",
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "user", "content": "two"},
                ],
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_max_tokens_over_cap_returns_422():
    app = create_app(_settings(max_output_tokens=8))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await _post_chat(
            client,
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 9,
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_unknown_chat_field_returns_422():
    app = create_app(_settings())
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await _post_chat(
            client,
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "hello"}],
                "top_p": 0.9,
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_pre_auth_ip_rate_limit_triggers_429_before_key_validation():
    app = create_app(_settings(pre_auth_rpm_per_ip=1))
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )

        def fail_validate(token: str):
            raise AssertionError("API key validation should be skipped")

        app.state.api_key_store.validate = fail_validate
        second = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )

    assert first.status_code == 401
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_streaming_concurrent_cap_triggers_429():
    provider = BlockingStreamProvider()
    app = create_app(_settings(max_concurrent_streams_per_key=1))
    app.dependency_overrides[get_provider_registry] = lambda: FakeRegistry(provider)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first_task = asyncio.create_task(
            _post_chat(
                client,
                {
                    "model": "gpt-test",
                    "messages": [{"role": "user", "content": "stream"}],
                    "stream": True,
                },
            )
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)

        second = await _post_chat(
            client,
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "stream"}],
                "stream": True,
            },
        )

        provider.release.set()
        first = await asyncio.wait_for(first_task, timeout=1)

    assert second.status_code == 429
    assert first.status_code == 200
    assert provider.closed is True


@pytest.mark.asyncio
async def test_streaming_deadline_aborts_cleanly():
    provider = SlowStreamProvider()
    app = create_app(_settings(stream_max_duration_seconds=1))
    app.dependency_overrides[get_provider_registry] = lambda: FakeRegistry(provider)
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await _post_chat(
            client,
            {
                "model": "gpt-test",
                "messages": [{"role": "user", "content": "stream"}],
                "stream": True,
            },
        )

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "stream_timeout" in response.text
    assert "data: [DONE]" not in response.text
    assert provider.closed is True
