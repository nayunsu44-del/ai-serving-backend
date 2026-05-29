from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.db.models import AuditLog
from app.errors import ProviderAPIError
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.pricing import calculate_cost
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


class PricedProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            id="chatcmpl-audit",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="ok"),
            finish_reason="stop",
            usage=NormalizedUsage(
                prompt_tokens=1_000_000,
                completion_tokens=2_000_000,
                total_tokens=3_000_000,
            ),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        yield NormalizedStreamChunk(
            id="chatcmpl-audit-stream",
            model=request.model,
            role="assistant",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-audit-stream",
            model=request.model,
            delta="ok",
            finish_reason="stop",
            usage=NormalizedUsage(
                prompt_tokens=1_000_000,
                completion_tokens=2_000_000,
                total_tokens=3_000_000,
            ),
        )


class InitialTimeoutProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.closed = False

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        raise AssertionError("not used")

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        try:
            await asyncio.sleep(2)
            yield NormalizedStreamChunk(
                id="chatcmpl-initial-timeout",
                model=request.model,
                role="assistant",
            )
        finally:
            self.closed = True


class MidStreamProviderErrorProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        raise AssertionError("not used")

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        yield NormalizedStreamChunk(
            id="chatcmpl-midstream-error",
            model=request.model,
            role="assistant",
        )
        raise ProviderAPIError(
            provider=self.name,
            upstream_status=500,
            raw_message="raw upstream stream secret",
        )


class PricedRegistry:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


async def _drain_audit_tasks(app) -> None:
    tasks = tuple(getattr(app.state, "audit_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_chat_completion_writes_audit_log_with_tokens_and_cost(
    app,
    client,
    auth_headers,
):
    provider = PricedProvider()
    app.dependency_overrides[get_provider_registry] = lambda: PricedRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "audit this"}],
        },
    )

    assert response.status_code == 200
    await _drain_audit_tasks(app)

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
        )
        audit_log = result.scalar_one()

    assert audit_log.status_code == 200
    assert audit_log.principal_hash is not None
    assert audit_log.provider == "fake"
    assert audit_log.model == "gpt-4o-mini"
    assert audit_log.prompt_tokens == 1_000_000
    assert audit_log.completion_tokens == 2_000_000
    assert audit_log.total_tokens == 3_000_000
    assert audit_log.cost_usd == calculate_cost("gpt-4o-mini", 1_000_000, 2_000_000)
    assert audit_log.stream is False


@pytest.mark.asyncio
async def test_streaming_chat_completion_writes_final_usage_to_audit_log(
    app,
    client,
    auth_headers,
):
    provider = PricedProvider()
    app.dependency_overrides[get_provider_registry] = lambda: PricedRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "audit streamed usage"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    await _drain_audit_tasks(app)

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
        )
        audit_log = result.scalar_one()

    assert audit_log.status_code == 200
    assert audit_log.prompt_tokens == 1_000_000
    assert audit_log.completion_tokens == 2_000_000
    assert audit_log.total_tokens == 3_000_000
    assert audit_log.cost_usd == calculate_cost("gpt-4o-mini", 1_000_000, 2_000_000)
    assert audit_log.stream is True


@pytest.mark.asyncio
async def test_initial_streaming_timeout_writes_504_to_response_and_audit(
    app,
    client,
    auth_headers,
):
    app.state.settings.stream_max_duration_seconds = 1
    provider = InitialTimeoutProvider()
    app.dependency_overrides[get_provider_registry] = lambda: PricedRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "timeout before first chunk"}],
            "stream": True,
        },
    )

    assert response.status_code == 504
    assert "stream_timeout" in response.text
    assert provider.closed is True
    await _drain_audit_tasks(app)

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
        )
        audit_log = result.scalar_one()

    assert audit_log.status_code == 504
    assert audit_log.error_type == "stream_timeout"
    assert audit_log.stream is True


@pytest.mark.asyncio
async def test_midstream_provider_error_writes_error_status_to_audit(
    app,
    client,
    auth_headers,
):
    provider = MidStreamProviderErrorProvider()
    app.dependency_overrides[get_provider_registry] = lambda: PricedRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "midstream failure"}],
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert "Upstream provider error" in response.text
    assert "raw upstream stream secret" not in response.text
    await _drain_audit_tasks(app)

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
        )
        audit_log = result.scalar_one()

    assert audit_log.status_code == 502
    assert audit_log.error_type == "provider_error"
    assert audit_log.stream is True
