from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from anthropic import APIError as AnthropicAPIError
from anthropic import AsyncAnthropic

from app.errors import ProviderAPIError, ProviderConfigurationError
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider

PROVIDER_TIMEOUT = httpx.Timeout(120.0, connect=5.0, read=60.0)
FINISH_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _extract_upstream_status(exc: AnthropicAPIError) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _extract_retry_after(exc: AnthropicAPIError) -> str | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if headers is None:
        return None
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    return str(retry_after) if retry_after else None


def _usage_token(usage: object | None, key: str) -> int:
    if usage is None:
        return 0
    if isinstance(usage, dict):
        value = usage.get(key)
    else:
        value = getattr(usage, key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None, default_max_tokens: int) -> None:
        self._api_key = api_key
        self._default_max_tokens = default_max_tokens
        self._client: AsyncAnthropic | None = None

    def _get_client(self) -> AsyncAnthropic:
        if not self._api_key:
            raise ProviderConfigurationError("Anthropic provider is not configured")
        if self._client is None:
            self._client = AsyncAnthropic(
                api_key=self._api_key,
                timeout=PROVIDER_TIMEOUT,
                max_retries=1,
            )
        return self._client

    def _to_anthropic_params(self, request: NormalizedChatRequest) -> dict:
        system_messages: list[str] = []
        messages: list[dict[str, str]] = []
        for message in request.messages:
            if message.role == "system":
                system_messages.append(message.content)
            else:
                messages.append({"role": message.role, "content": message.content})

        params = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or self._default_max_tokens,
        }
        if system_messages:
            params["system"] = "\n\n".join(system_messages)
        if request.temperature is not None:
            params["temperature"] = request.temperature
        return params

    def _provider_error(self, exc: AnthropicAPIError) -> ProviderAPIError:
        return ProviderAPIError(
            provider=self.name,
            upstream_status=_extract_upstream_status(exc),
            raw_message=str(exc),
            raw_error_class=type(exc).__name__,
            retry_after=_extract_retry_after(exc),
        )

    @staticmethod
    def _normalize_finish_reason(reason: str | None) -> str | None:
        if reason is None:
            return None
        return FINISH_REASON_MAP.get(reason, "stop")

    @staticmethod
    def _extract_text(content: object) -> str:
        parts: list[str] = []
        for block in content or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        try:
            response = await self._get_client().messages.create(
                **self._to_anthropic_params(request)
            )
        except AnthropicAPIError as exc:
            raise self._provider_error(exc) from exc

        input_tokens = _usage_token(response.usage, "input_tokens")
        output_tokens = _usage_token(response.usage, "output_tokens")

        return NormalizedChatResponse(
            id=response.id,
            model=response.model,
            message=NormalizedMessage(
                role="assistant",
                content=self._extract_text(response.content),
            ),
            finish_reason=self._normalize_finish_reason(response.stop_reason),
            usage=NormalizedUsage(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        try:
            async with self._get_client().with_options(max_retries=0).messages.stream(
                **self._to_anthropic_params(request)
            ) as stream:
                async for text in stream.text_stream:
                    yield NormalizedStreamChunk(model=request.model, delta=text)

                final_message = await stream.get_final_message()
                input_tokens = _usage_token(final_message.usage, "input_tokens")
                output_tokens = _usage_token(final_message.usage, "output_tokens")
                yield NormalizedStreamChunk(
                    id=final_message.id,
                    model=final_message.model,
                    finish_reason=self._normalize_finish_reason(final_message.stop_reason),
                    usage=NormalizedUsage(
                        prompt_tokens=input_tokens,
                        completion_tokens=output_tokens,
                        total_tokens=input_tokens + output_tokens,
                    ),
                )
        except AnthropicAPIError as exc:
            raise self._provider_error(exc) from exc
