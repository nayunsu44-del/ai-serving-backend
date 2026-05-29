from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.compliance.pii import mask_messages, mask_text
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


ALL_TYPES = ["rrn", "card", "phone", "email"]


class CaptureProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.last_request: NormalizedChatRequest | None = None

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        self.last_request = request
        return NormalizedChatResponse(
            id="chatcmpl-pii",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="ok"),
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        raise AssertionError("not used")
        yield NormalizedStreamChunk(model=request.model)


class CaptureRegistry:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


def test_rrn_masks_valid_date_but_not_invalid_date() -> None:
    masked, counts = mask_text(
        "valid 900101-1234567 invalid 901301-1234567",
        ALL_TYPES,
    )

    assert masked == "valid [REDACTED:RRN:1] invalid 901301-1234567"
    assert counts == {"rrn": 1}


def test_rrn_masks_only_real_calendar_dates_for_encoded_century() -> None:
    masked, counts = mask_text(
        "invalid 900231-1234567 leap 000229-3234567 nonleap 000229-1234567",
        ALL_TYPES,
    )

    assert masked == (
        "invalid 900231-1234567 leap [REDACTED:RRN:1] "
        "nonleap 000229-1234567"
    )
    assert counts == {"rrn": 1}


def test_card_masks_only_luhn_valid_numbers() -> None:
    masked, counts = mask_text(
        "valid 4111111111111111 invalid 4111111111111112",
        ALL_TYPES,
    )

    assert masked == "valid [REDACTED:CARD:1] invalid 4111111111111112"
    assert counts == {"card": 1}


def test_phone_and_email_are_masked() -> None:
    masked, counts = mask_text(
        "mobile 010-1234-5678 office 02 123 4567 email user.name+test@example.co.kr",
        ALL_TYPES,
    )

    assert masked == (
        "mobile [REDACTED:PHONE:1] office [REDACTED:PHONE:2] "
        "email [REDACTED:EMAIL:1]"
    )
    assert counts == {"email": 1, "phone": 2}


def test_same_rrn_reuses_placeholder_and_counts_occurrences() -> None:
    masked, counts = mask_text(
        "first 900101-1234567 second 900101-1234567",
        ALL_TYPES,
    )

    assert masked == "first [REDACTED:RRN:1] second [REDACTED:RRN:1]"
    assert counts == {"rrn": 2}


def test_different_cards_get_distinct_placeholders() -> None:
    masked, counts = mask_text(
        "cards 4111111111111111 and 4012888888881881",
        ALL_TYPES,
    )

    assert masked == "cards [REDACTED:CARD:1] and [REDACTED:CARD:2]"
    assert counts == {"card": 2}


def test_enabled_types_subset_only_masks_requested_types() -> None:
    masked, counts = mask_text(
        "call 010-1234-5678 or email person@example.com",
        ["unknown", "email"],
    )

    assert masked == "call 010-1234-5678 or email [REDACTED:EMAIL:1]"
    assert counts == {"email": 1}


def test_mask_messages_preserves_order_roles_and_does_not_mutate_inputs() -> None:
    messages = [
        NormalizedMessage(role="system", content="admin 900101-1234567"),
        NormalizedMessage(role="user", content="mail user@example.com"),
        NormalizedMessage(role="assistant", content="phone 01012345678"),
    ]

    masked_messages, counts = mask_messages(messages, ALL_TYPES)

    assert [message.role for message in masked_messages] == [
        "system",
        "user",
        "assistant",
    ]
    assert [message.content for message in masked_messages] == [
        "admin [REDACTED:RRN:1]",
        "mail [REDACTED:EMAIL:1]",
        "phone [REDACTED:PHONE:1]",
    ]
    assert counts == {"rrn": 1, "email": 1, "phone": 1}
    assert [message.content for message in messages] == [
        "admin 900101-1234567",
        "mail user@example.com",
        "phone 01012345678",
    ]
    assert masked_messages is not messages
    assert all(masked is not original for masked, original in zip(masked_messages, messages))


def test_clean_message_is_unchanged_with_empty_counts() -> None:
    masked, counts = mask_text("no personal data here", ALL_TYPES)

    assert masked == "no personal data here"
    assert counts == {}


@pytest.mark.asyncio
async def test_chat_completion_masks_pii_before_provider_call(
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
            "messages": [{"role": "user", "content": "rrn 900101-1234567"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
    assert provider.last_request.messages[0].content == "rrn [REDACTED:RRN:1]"
