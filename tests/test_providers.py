from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.errors import ProviderAPIError
from app.normalized import NormalizedChatRequest, NormalizedMessage
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.openai_provider import OpenAIProvider


def _request(model: str) -> NormalizedChatRequest:
    return NormalizedChatRequest(
        model=model,
        messages=[NormalizedMessage(role="user", content="hello")],
    )


class FakeOpenAICompletions:
    def __init__(self, response: object | None = None) -> None:
        self.response = response
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.response is not None:
            return self.response

        async def events():
            yield SimpleNamespace(
                id="chatcmpl-test",
                model=kwargs["model"],
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(role="assistant", content="hello"),
                        finish_reason=None,
                    )
                ],
                usage=None,
            )

        return events()


class FakeOpenAIClient:
    def __init__(self, completions: FakeOpenAICompletions) -> None:
        self.completions = completions
        self.chat = SimpleNamespace(completions=completions)
        self.options: list[dict] = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


@pytest.mark.asyncio
async def test_openai_streaming_requests_usage(monkeypatch):
    provider = OpenAIProvider("test-key")
    completions = FakeOpenAICompletions()
    client = FakeOpenAIClient(completions)
    monkeypatch.setattr(provider, "_get_client", lambda: client)

    chunks = [chunk async for chunk in provider.chat_stream(_request("gpt-test"))]

    assert chunks[0].delta == "hello"
    assert client.options == [{"max_retries": 0}]
    assert completions.calls[0]["stream"] is True
    assert completions.calls[0]["stream_options"] == {"include_usage": True}


@pytest.mark.asyncio
async def test_openai_non_streaming_empty_choices_raises_provider_error(monkeypatch):
    provider = OpenAIProvider("test-key")
    response = SimpleNamespace(
        id="chatcmpl-empty",
        model="gpt-test",
        choices=[],
        usage=None,
    )
    client = FakeOpenAIClient(FakeOpenAICompletions(response))
    monkeypatch.setattr(provider, "_get_client", lambda: client)

    with pytest.raises(ProviderAPIError) as exc_info:
        await provider.chat(_request("gpt-test"))

    assert exc_info.value.message == "Upstream provider error"
    assert exc_info.value.raw_message == "OpenAI response contained no choices"


class FakeAnthropicMessages:
    async def create(self, **kwargs):
        return SimpleNamespace(
            id="msg-test",
            model=kwargs["model"],
            content=[SimpleNamespace(text="hello")],
            stop_reason="tool_use",
            usage=SimpleNamespace(input_tokens=1, output_tokens=2),
        )

    def stream(self, **kwargs):
        return FakeAnthropicStream(kwargs["model"])


class FakeAnthropicStream:
    def __init__(self, model: str) -> None:
        self.model = model

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    @property
    def text_stream(self):
        async def events():
            yield "hello"

        return events()

    async def get_final_message(self):
        return SimpleNamespace(
            id="msg-final",
            model=self.model,
            stop_reason="max_tokens",
            usage=SimpleNamespace(input_tokens=3, output_tokens=4),
        )


class FakeAnthropicClient:
    def __init__(self) -> None:
        self.messages = FakeAnthropicMessages()
        self.options: list[dict] = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


class FakeAnthropicNoneUsageMessages:
    async def create(self, **kwargs):
        return SimpleNamespace(
            id="msg-none-usage",
            model=kwargs["model"],
            content=[SimpleNamespace(text="hello")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=None, output_tokens=None),
        )

    def stream(self, **kwargs):
        return FakeAnthropicNoneUsageStream(kwargs["model"])


class FakeAnthropicNoneUsageStream:
    def __init__(self, model: str) -> None:
        self.model = model

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    @property
    def text_stream(self):
        async def events():
            yield "hello"

        return events()

    async def get_final_message(self):
        return SimpleNamespace(
            id="msg-final-none-usage",
            model=self.model,
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=None, output_tokens=None),
        )


class FakeAnthropicNoneUsageClient(FakeAnthropicClient):
    def __init__(self) -> None:
        self.messages = FakeAnthropicNoneUsageMessages()
        self.options: list[dict] = []


@pytest.mark.parametrize(
    ("source", "normalized"),
    [
        ("end_turn", "stop"),
        ("max_tokens", "length"),
        ("stop_sequence", "stop"),
        ("tool_use", "tool_calls"),
        ("unexpected", "stop"),
    ],
)
def test_anthropic_finish_reason_map(source: str, normalized: str):
    assert AnthropicProvider._normalize_finish_reason(source) == normalized


@pytest.mark.asyncio
async def test_anthropic_non_streaming_finish_reason_is_normalized(monkeypatch):
    provider = AnthropicProvider("test-key", default_max_tokens=1024)
    monkeypatch.setattr(provider, "_get_client", lambda: FakeAnthropicClient())

    response = await provider.chat(_request("claude-test"))

    assert response.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_anthropic_non_streaming_none_usage_fields_are_zero(monkeypatch):
    provider = AnthropicProvider("test-key", default_max_tokens=1024)
    monkeypatch.setattr(provider, "_get_client", lambda: FakeAnthropicNoneUsageClient())

    response = await provider.chat(_request("claude-test"))

    assert response.usage.prompt_tokens == 0
    assert response.usage.completion_tokens == 0
    assert response.usage.total_tokens == 0


@pytest.mark.asyncio
async def test_anthropic_streaming_finish_reason_is_normalized(monkeypatch):
    provider = AnthropicProvider("test-key", default_max_tokens=1024)
    client = FakeAnthropicClient()
    monkeypatch.setattr(provider, "_get_client", lambda: client)

    chunks = [chunk async for chunk in provider.chat_stream(_request("claude-test"))]

    assert chunks[-1].finish_reason == "length"
    assert client.options == [{"max_retries": 0}]


@pytest.mark.asyncio
async def test_anthropic_streaming_none_usage_fields_are_zero(monkeypatch):
    provider = AnthropicProvider("test-key", default_max_tokens=1024)
    monkeypatch.setattr(provider, "_get_client", lambda: FakeAnthropicNoneUsageClient())

    chunks = [chunk async for chunk in provider.chat_stream(_request("claude-test"))]

    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 0
    assert chunks[-1].usage.completion_tokens == 0
    assert chunks[-1].usage.total_tokens == 0
