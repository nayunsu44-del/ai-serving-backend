from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.db.models import AuditLog
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
CARD = "4111111111111111"
PHONE = "010-1234-5678"
EMAIL = "hong@example.com"
RAW_PII_VALUES = (RRN, CARD, PHONE, EMAIL)
PLACEHOLDER_TYPES = ("RRN", "CARD", "PHONE", "EMAIL")


class CaptureProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.last_request: NormalizedChatRequest | None = None

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        self.last_request = request
        return NormalizedChatResponse(
            id="chatcmpl-pii-e2e",
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
            id="chatcmpl-pii-e2e-stream",
            model=request.model,
            role="assistant",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-pii-e2e-stream",
            model=request.model,
            delta="ok",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-pii-e2e-stream",
            model=request.model,
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


def _multi_role_pii_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": f"고객 정보: {RRN}"},
        {"role": "user", "content": f"내 카드 {CARD} 그리고 전화 {PHONE}"},
        {"role": "assistant", "content": f"메일 {EMAIL} 로 연락"},
    ]


def _all_message_content(request: NormalizedChatRequest) -> str:
    return " ".join(message.content for message in request.messages)


def _assert_no_raw_pii(text: str) -> None:
    for raw_value in RAW_PII_VALUES:
        assert raw_value not in text


def _assert_all_placeholder_types(text: str) -> None:
    for pii_type in PLACEHOLDER_TYPES:
        assert f"[REDACTED:{pii_type}:" in text


def _placeholder_types(text: str) -> set[str]:
    return {
        pii_type
        for pii_type in PLACEHOLDER_TYPES
        if f"[REDACTED:{pii_type}:" in text
    }


def _log_record_text(record: logging.LogRecord) -> str:
    return " ".join(
        (
            record.getMessage(),
            str(record.args),
            str(getattr(record, "extra_fields", "")),
        )
    )


@pytest.mark.asyncio
async def test_pii_masked_in_provider_payload_non_streaming(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": _multi_role_pii_messages(),
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
    content = _all_message_content(provider.last_request)
    _assert_no_raw_pii(content)
    _assert_all_placeholder_types(content)
    assert [message.role for message in provider.last_request.messages] == [
        "system",
        "user",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_pii_masked_in_provider_payload_streaming(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": _multi_role_pii_messages(),
            "stream": True,
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text
    assert provider.last_request is not None
    content = _all_message_content(provider.last_request)
    _assert_no_raw_pii(content)
    _assert_all_placeholder_types(content)


@pytest.mark.asyncio
async def test_pii_disabled_passes_raw_to_provider(app, client, auth_headers) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.pii_masking_enabled = False

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": f"rrn {RRN}"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
    assert provider.last_request.messages[0].content == f"rrn {RRN}"
    assert "[REDACTED:" not in provider.last_request.messages[0].content


@pytest.mark.asyncio
async def test_pii_types_subset_only_masks_selected(app, client, auth_headers) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.pii_types = ["email"]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [
                {"role": "user", "content": f"email {EMAIL} rrn {RRN}"},
            ],
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
    content = provider.last_request.messages[0].content
    assert "[REDACTED:EMAIL:1]" in content
    assert EMAIL not in content
    assert RRN in content
    assert "[REDACTED:RRN:" not in content


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_audit_log_contains_no_raw_pii(
    app,
    client,
    auth_headers,
    stream: bool,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [
                {
                    "role": "user",
                    "content": f"rrn {RRN} card {CARD} phone {PHONE} email {EMAIL}",
                },
            ],
            "stream": stream,
        },
    )

    assert response.status_code == 200
    if stream:
        assert response.text
    await _drain_audit_tasks(app)

    async with app.state.db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
        )
        row = result.scalar_one()

    row_text = " ".join(
        str(getattr(row, column.name)) for column in AuditLog.__table__.columns
    )
    _assert_no_raw_pii(row_text)
    assert row.stream == stream
    assert row.status_code == 200


@pytest.mark.asyncio
async def test_pii_not_in_structured_logs(app, client, auth_headers, caplog) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    caplog.set_level(logging.DEBUG)
    captured_records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_records.append(record)

    root_logger = logging.getLogger()
    handler = CaptureHandler()
    root_logger.addHandler(handler)
    try:
        response = await client.post(
            "/v1/chat/completions",
            headers=auth_headers,
            json={
                "model": "gpt-test",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"rrn {RRN} card {CARD} phone {PHONE} email {EMAIL}"
                        ),
                    },
                ],
            },
        )
    finally:
        root_logger.removeHandler(handler)

    assert response.status_code == 200
    records = [*caplog.records, *captured_records]
    assert records
    log_text = " ".join(_log_record_text(record) for record in records)
    _assert_no_raw_pii(log_text)
    pii_masked_records = [
        record for record in records if record.getMessage() == "pii_masked"
    ]
    assert pii_masked_records
    for record in pii_masked_records:
        assert getattr(record, "extra_fields", {}).get("counts") == {
            "rrn": 1,
            "card": 1,
            "phone": 1,
            "email": 1,
        }


@pytest.mark.asyncio
async def test_streaming_and_non_streaming_mask_identically(
    app,
    client,
    auth_headers,
) -> None:
    non_streaming_provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(
        non_streaming_provider
    )

    non_streaming_response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": _multi_role_pii_messages(),
            "stream": False,
        },
    )

    streaming_provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(
        streaming_provider
    )

    streaming_response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": _multi_role_pii_messages(),
            "stream": True,
        },
    )

    assert non_streaming_response.status_code == 200
    assert streaming_response.status_code == 200
    assert non_streaming_provider.last_request is not None
    assert streaming_provider.last_request is not None
    non_streaming_content = _all_message_content(non_streaming_provider.last_request)
    streaming_content = _all_message_content(streaming_provider.last_request)
    _assert_no_raw_pii(non_streaming_content)
    _assert_no_raw_pii(streaming_content)
    assert _placeholder_types(non_streaming_content) == {
        "RRN",
        "CARD",
        "PHONE",
        "EMAIL",
    }
    assert _placeholder_types(streaming_content) == _placeholder_types(
        non_streaming_content
    )


@pytest.mark.asyncio
async def test_multi_turn_conversation_roundtrip_non_streaming(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    messages = [
        {"role": "system", "content": "은행 상담 어시스턴트"},
        {"role": "user", "content": "예금 금리를 비교해줘"},
        {"role": "assistant", "content": "기간과 금액을 알려주세요"},
        {"role": "user", "content": "12개월, 천만원"},
    ]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": 32,
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
    assert [
        {"role": message.role, "content": message.content}
        for message in provider.last_request.messages
    ] == messages
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "ok",
    }
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
    }
