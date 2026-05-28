from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import fields

import pytest

from app.compliance.filter import PolicyMatch, compile_rules, scan_messages
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
POLICY_403_BODY = {
    "error": {
        "message": "Request blocked by content policy.",
        "type": "content_policy_violation",
        "param": None,
        "code": "content_policy_violation",
    }
}


class CaptureProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.last_request: NormalizedChatRequest | None = None

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        self.last_request = request
        return NormalizedChatResponse(
            id="chatcmpl-policy",
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
            id="chatcmpl-policy-stream",
            model=request.model,
            role="assistant",
        )
        yield NormalizedStreamChunk(
            id="chatcmpl-policy-stream",
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


def _all_message_content(request: NormalizedChatRequest) -> str:
    return " ".join(message.content for message in request.messages)


def test_compile_rules_ignores_invalid_entries_and_bad_regex() -> None:
    rules = compile_rules(["valid=secret", "missing_separator", "bad=("])

    assert len(rules) == 1
    assert rules[0].rule_id == "valid"
    assert rules[0].pattern.search("SECRET")


def test_scan_messages_aggregates_counts_without_raw_text() -> None:
    rules = compile_rules(["alpha=secret", "beta=wire fraud", "unused=missing"])
    messages = [
        NormalizedMessage(role="user", content="secret wire fraud"),
        NormalizedMessage(role="assistant", content="SECRET"),
    ]

    matches = scan_messages(messages, rules)

    assert matches == [
        PolicyMatch(rule_id="alpha", count=2, severity="medium"),
        PolicyMatch(rule_id="beta", count=1, severity="medium"),
    ]
    assert [field.name for field in fields(PolicyMatch)] == [
        "rule_id",
        "count",
        "severity",
    ]
    assert "secret" not in repr(matches).lower()
    assert "wire fraud" not in repr(matches).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_block_mode_forbidden_content_returns_403_without_provider_call(
    app,
    client,
    auth_headers,
    stream: bool,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.policy_mode = "block"
    app.state.settings.forbidden_patterns = ["forbidden=blocked phrase"]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "this has a blocked phrase"}],
            "stream": stream,
        },
    )

    assert response.status_code == 403
    assert response.json() == POLICY_403_BODY
    assert provider.last_request is None


@pytest.mark.asyncio
async def test_log_only_forbidden_content_calls_provider_with_pii_masked_payload(
    app,
    client,
    auth_headers,
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
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
    content = _all_message_content(provider.last_request)
    assert RRN not in content
    assert "[REDACTED:RRN:1]" in content


@pytest.mark.asyncio
async def test_disabled_mode_skips_forbidden_filter(app, client, auth_headers) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.policy_mode = "disabled"
    app.state.settings.forbidden_patterns = ["forbidden=blocked phrase"]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "blocked phrase"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None


@pytest.mark.asyncio
async def test_no_forbidden_patterns_configured_is_inert(
    app,
    client,
    auth_headers,
) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.policy_mode = "block"
    app.state.settings.forbidden_patterns = []

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "blocked phrase"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None


@pytest.mark.asyncio
async def test_clean_message_in_block_mode_calls_provider(app, client, auth_headers) -> None:
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)
    app.state.settings.policy_mode = "block"
    app.state.settings.forbidden_patterns = ["forbidden=blocked phrase"]

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "ordinary request"}],
        },
    )

    assert response.status_code == 200
    assert provider.last_request is not None
