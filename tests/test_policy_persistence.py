from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.db.models import AuditLog, AuditMessage, PolicyEvent
from app.errors import UnsupportedModelError
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


RRN = "900101-1234567"
EMAIL = "hong@example.com"


class CaptureProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.last_request: NormalizedChatRequest | None = None

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        self.last_request = request
        return NormalizedChatResponse(
            id="chatcmpl-policy-persistence",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="ok"),
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        self.last_request = request
        yield NormalizedStreamChunk(
            id="chatcmpl-policy-persistence-stream",
            model=request.model,
            role="assistant",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-policy-persistence-stream",
            model=request.model,
            delta="ok",
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=4, completion_tokens=2, total_tokens=6),
        )


class CaptureRegistry:
    def __init__(self, provider: CaptureProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        if model == "unsupported-model":
            raise UnsupportedModelError(f"Unsupported model: {model}")
        return self.provider

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="gpt-test", owned_by="fake")]


async def _drain_audit_tasks(app) -> None:
    tasks = tuple(getattr(app.state, "audit_tasks", set()))
    if tasks:
        await asyncio.gather(*tasks)


def _row_columns_text(rows: list[object]) -> str:
    return " ".join(
        str(getattr(row, column.name))
        for row in rows
        for column in row.__table__.columns
    )


async def _policy_events(app, request_id: str) -> list[PolicyEvent]:
    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(PolicyEvent)
            .where(PolicyEvent.request_id == request_id)
            .order_by(PolicyEvent.event_type, PolicyEvent.rule_id)
        )
        return list(result.scalars().all())


async def _audit_messages(app, request_id: str) -> list[AuditMessage]:
    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditMessage)
            .where(AuditMessage.request_id == request_id)
            .order_by(AuditMessage.seq)
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_forbidden_log_only_persists_policy_event(
    app,
    client,
    auth_headers,
    stream: bool,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.policy_mode = "log_only"
    app.state.settings.forbidden_patterns = ["forbidden=blocked phrase"]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [
                {
                    "role": "user",
                    "content": f"blocked phrase with rrn {RRN}",
                }
            ],
            "stream": stream,
        },
    )

    assert response.status_code == 200
    if stream:
        assert response.text
    await _drain_audit_tasks(app)

    events = await _policy_events(app, response.headers["x-request-id"])

    forbidden_event = next(
        event for event in events if event.event_type == "forbidden_content"
    )
    assert forbidden_event.action == "log"
    assert forbidden_event.rule_id == "forbidden"
    assert forbidden_event.count >= 1
    assert forbidden_event.severity == "medium"
    assert forbidden_event.stream == stream

    pii_event = next(event for event in events if event.event_type == "pii_mask")
    assert pii_event.rule_id == "rrn"
    assert pii_event.action == "mask"

    row_text = _row_columns_text(events)
    assert RRN not in row_text
    assert "blocked phrase" not in row_text


@pytest.mark.asyncio
async def test_forbidden_block_persists_block_event_and_no_messages(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.audit_store_messages = True
    app.state.settings.policy_mode = "block"
    app.state.settings.forbidden_patterns = ["forbidden=blocked phrase"]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "this has a blocked phrase"}],
        },
    )

    assert response.status_code == 403
    await _drain_audit_tasks(app)
    request_id = response.headers["x-request-id"]

    events = await _policy_events(app, request_id)
    forbidden_event = next(
        event for event in events if event.event_type == "forbidden_content"
    )
    assert forbidden_event.action == "block"

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == request_id)
        )
        audit_log = result.scalar_one()
    assert audit_log.status_code == 403
    assert audit_log.error_type == "content_policy_violation"

    messages = await _audit_messages(app, request_id)
    assert messages == []


@pytest.mark.asyncio
async def test_audit_store_messages_persists_masked_only(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.audit_store_messages = True
    app.state.settings.forbidden_patterns = []

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": f"rrn {RRN}"}],
        },
    )

    assert response.status_code == 200
    await _drain_audit_tasks(app)

    messages = await _audit_messages(app, response.headers["x-request-id"])
    assert messages
    assert [message.seq for message in messages] == [0]
    assert [message.role for message in messages] == ["user"]
    assert "[REDACTED:RRN:1]" in messages[0].content
    assert RRN not in messages[0].content


@pytest.mark.asyncio
async def test_audit_store_messages_disabled_stores_nothing(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.forbidden_patterns = []

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": f"rrn {RRN}"}],
        },
    )

    assert response.status_code == 200
    await _drain_audit_tasks(app)

    messages = await _audit_messages(app, response.headers["x-request-id"])
    assert messages == []


@pytest.mark.asyncio
async def test_pii_mask_events_recorded_without_forbidden(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.forbidden_patterns = []

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [
                {"role": "user", "content": f"rrn {RRN} email {EMAIL}"},
            ],
        },
    )

    assert response.status_code == 200
    await _drain_audit_tasks(app)

    events = await _policy_events(app, response.headers["x-request-id"])
    pii_events = {event.rule_id: event for event in events if event.event_type == "pii_mask"}
    assert set(pii_events) == {"rrn", "email"}
    assert all(event.action == "mask" for event in pii_events.values())
    assert not any(event.event_type == "forbidden_content" for event in events)

    row_text = _row_columns_text(events)
    assert RRN not in row_text
    assert EMAIL not in row_text
