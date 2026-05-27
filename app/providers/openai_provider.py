from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
from openai import APIError as OpenAIAPIError
from openai import AsyncOpenAI

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


def _extract_upstream_status(exc: OpenAIAPIError) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _extract_retry_after(exc: OpenAIAPIError) -> str | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if headers is None:
        return None
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    return str(retry_after) if retry_after else None


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self, api_key: str | None) -> None:
        self._api_key = api_key
        self._client: AsyncOpenAI | None = None

    def _get_client(self) -> AsyncOpenAI:
        if not self._api_key:
            raise ProviderConfigurationError("OpenAI provider is not configured")
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self._api_key,
                timeout=PROVIDER_TIMEOUT,
                max_retries=1,
            )
        return self._client

    @staticmethod
    def _to_openai_params(request: NormalizedChatRequest) -> dict:
        params = {
            "model": request.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in request.messages
            ],
        }
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        return params

    def _provider_error(self, exc: OpenAIAPIError) -> ProviderAPIError:
        return ProviderAPIError(
            provider=self.name,
            upstream_status=_extract_upstream_status(exc),
            raw_message=str(exc),
            raw_error_class=type(exc).__name__,
            retry_after=_extract_retry_after(exc),
        )

    @staticmethod
    def _usage_from_openai(usage: Any) -> NormalizedUsage:
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        total_tokens = getattr(usage, "total_tokens", 0) or prompt_tokens + completion_tokens
        return NormalizedUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        try:
            response = await self._get_client().chat.completions.create(
                **self._to_openai_params(request)
            )
        except OpenAIAPIError as exc:
            raise self._provider_error(exc) from exc

        if not response.choices:
            raise ProviderAPIError(
                provider=self.name,
                raw_message="OpenAI response contained no choices",
            )
        choice = response.choices[0]
        usage = self._usage_from_openai(response.usage)

        return NormalizedChatResponse(
            id=response.id,
            model=response.model,
            message=NormalizedMessage(
                role=choice.message.role or "assistant",
                content=choice.message.content or "",
            ),
            finish_reason=choice.finish_reason,
            usage=usage,
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        try:
            stream = await self._get_client().with_options(max_retries=0).chat.completions.create(
                **self._to_openai_params(request),
                stream=True,
                stream_options={"include_usage": True},
            )
            async for event in stream:
                usage = getattr(event, "usage", None)
                if not event.choices:
                    if usage is not None:
                        yield NormalizedStreamChunk(
                            id=event.id,
                            model=event.model,
                            usage=self._usage_from_openai(usage),
                        )
                    continue
                choice = event.choices[0]
                yield NormalizedStreamChunk(
                    id=event.id,
                    model=event.model,
                    role=getattr(choice.delta, "role", None),
                    delta=getattr(choice.delta, "content", None) or "",
                    finish_reason=choice.finish_reason,
                    usage=self._usage_from_openai(usage) if usage else None,
                )
        except OpenAIAPIError as exc:
            raise self._provider_error(exc) from exc
