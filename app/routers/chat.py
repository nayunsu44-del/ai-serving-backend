from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.auth import require_scope
from app.compliance.filter import compile_rules, scan_messages
from app.compliance.pii import mask_messages
from app.errors import (
    APIError,
    PolicyViolationError,
    ProviderAPIError,
    RateLimitError,
    openai_error_body,
)
from app.normalized import NormalizedChatRequest
from app.normalized import NormalizedStreamChunk
from app.observability import log_event
from app.providers.base import AIProvider
from app.providers.registry import ProviderRegistry, get_provider_registry
from app.ratelimit.base import enforce_rate_limit
from app.schemas import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    model_dump_json_line,
)
from app.streaming import StreamLease

router = APIRouter(prefix="/v1", tags=["chat"])
logger = logging.getLogger("ai_serving.chat")


def _sse_data(payload: str) -> str:
    return f"data: {payload}\n\n"


def _sse_event(event: str, payload: str) -> str:
    return f"event: {event}\ndata: {payload}\n\n"


def _stream_timeout_sse() -> str:
    payload = openai_error_body("Stream timed out", "server_error", "stream_timeout")
    return _sse_event("error", json.dumps(payload, ensure_ascii=False))


async def parse_chat_completion_request(request: Request) -> ChatCompletionRequest:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ("body",),
                    "msg": "Invalid JSON",
                    "input": None,
                }
            ]
        ) from exc

    try:
        payload = ChatCompletionRequest.model_validate(
            body,
            context={"settings": request.app.state.settings},
        )
        request.state.stream = payload.stream
        return payload
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


async def _close_upstream_stream(
    upstream_stream: AsyncIterator[NormalizedStreamChunk],
) -> None:
    aclose = getattr(upstream_stream, "aclose", None)
    if callable(aclose):
        with contextlib.suppress(Exception):
            await aclose()


async def _next_chunk_with_deadline(
    upstream_stream: AsyncIterator[NormalizedStreamChunk],
    deadline: float,
) -> NormalizedStreamChunk:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(anext(upstream_stream), timeout=remaining)


async def _stream_timeout_only(
    provider: AIProvider,
    normalized: NormalizedChatRequest,
    request: Request,
) -> AsyncIterator[str]:
    request.state.error_type = "stream_timeout"
    request.state.audit_status_code = 504
    log_event(
        logger,
        "chat_stream_timeout",
        request_id=getattr(request.state, "request_id", None),
        provider=provider.name,
        model=normalized.model,
        status=504,
    )
    yield _stream_timeout_sse()


async def _stream_chunks(
    provider: AIProvider,
    normalized: NormalizedChatRequest,
    request: Request,
    first_chunk: NormalizedStreamChunk | None,
    upstream_stream: AsyncIterator[NormalizedStreamChunk],
    stream_lease: StreamLease,
    deadline: float,
) -> AsyncIterator[str]:
    fallback_id = f"chatcmpl-{uuid.uuid4().hex}"
    token_usage: dict | None = None

    def to_sse(chunk: NormalizedStreamChunk) -> str:
        nonlocal token_usage
        if chunk.usage is not None:
            token_usage = chunk.usage.model_dump()
        openai_chunk = ChatCompletionChunk.from_normalized(
            chunk,
            fallback_id=fallback_id,
            fallback_model=normalized.model,
        )
        return _sse_data(model_dump_json_line(openai_chunk))

    try:
        if first_chunk is not None:
            yield to_sse(first_chunk)

        while True:
            try:
                chunk = await _next_chunk_with_deadline(upstream_stream, deadline)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                request.state.error_type = "stream_timeout"
                request.state.audit_status_code = 504
                log_event(
                    logger,
                    "chat_stream_timeout",
                    request_id=getattr(request.state, "request_id", None),
                    provider=provider.name,
                    model=normalized.model,
                    status=504,
                )
                yield _stream_timeout_sse()
                return

            yield to_sse(chunk)

        request.state.token_usage = token_usage
        log_event(
            logger,
            "chat_stream_complete",
            request_id=getattr(request.state, "request_id", None),
            provider=provider.name,
            model=normalized.model,
            status=200,
            token_usage=token_usage,
        )
        yield _sse_data("[DONE]")
    except APIError as exc:
        request.state.error_type = exc.error_type
        request.state.audit_status_code = exc.status_code
        log_event(
            logger,
            "chat_stream_error",
            request_id=getattr(request.state, "request_id", None),
            provider=provider.name,
            model=normalized.model,
            status=exc.status_code,
            error_type=exc.error_type,
        )
        if isinstance(exc, ProviderAPIError):
            level = (
                logging.ERROR
                if exc.upstream_status is None or exc.upstream_status >= 500
                else logging.WARNING
            )
            logger.log(
                level,
                "Upstream provider stream error",
                extra={
                    "extra_fields": {
                        "request_id": getattr(request.state, "request_id", None),
                        "provider_error_class": exc.raw_error_class or type(exc).__name__,
                        "upstream_status": exc.upstream_status,
                    }
                },
            )
        payload = openai_error_body(exc.message, exc.error_type, exc.code)
        yield _sse_data(json.dumps(payload, ensure_ascii=False))
    except Exception:
        request.state.error_type = "server_error"
        request.state.audit_status_code = 500
        logger.exception(
            "Unhandled provider stream error",
            extra={
                "extra_fields": {
                    "request_id": getattr(request.state, "request_id", None),
                    "provider": provider.name,
                    "model": normalized.model,
                }
            },
        )
        payload = openai_error_body(
            "Internal server error",
            "server_error",
            "internal_error",
        )
        yield _sse_data(json.dumps(payload, ensure_ascii=False))
    finally:
        await _close_upstream_stream(upstream_stream)
        await stream_lease.release()


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: Request,
    principal=Depends(enforce_rate_limit),
    _chat_scope=Depends(require_scope("chat")),
    payload: ChatCompletionRequest = Depends(parse_chat_completion_request),
    registry: ProviderRegistry = Depends(get_provider_registry),
) -> ChatCompletionResponse | StreamingResponse:
    normalized = payload.to_normalized()
    settings = request.app.state.settings
    request.state.model = normalized.model
    request.state.stream = normalized.stream

    if settings.policy_mode != "disabled" and settings.forbidden_patterns:
        rules = compile_rules(settings.forbidden_patterns)
        matches = scan_messages(normalized.messages, rules)
        if matches:
            action = "block" if settings.policy_mode == "block" else "log"
            events = [
                {
                    "event_type": "forbidden_content",
                    "action": action,
                    "rule_id": match.rule_id,
                    "count": match.count,
                    "severity": match.severity,
                }
                for match in matches
            ]
            existing_events = getattr(request.state, "policy_events", [])
            request.state.policy_events = existing_events + events
            log_event(
                logger,
                "policy_event",
                request_id=getattr(request.state, "request_id", None),
                mode=settings.policy_mode,
                action=action,
                matches=[
                    {
                        "rule_id": match.rule_id,
                        "count": match.count,
                        "severity": match.severity,
                    }
                    for match in matches
                ],
            )
            if settings.policy_mode == "block":
                request.state.error_type = "content_policy_violation"
                raise PolicyViolationError()

    if settings.pii_masking_enabled:
        masked_messages, pii_summary = mask_messages(normalized.messages, settings.pii_types)
        normalized.messages = masked_messages
        request.state.stored_messages = [
            {"seq": i, "role": message.role, "content": message.content}
            for i, message in enumerate(masked_messages)
        ]
        request.state.pii_masked = pii_summary
        if pii_summary:
            log_event(
                logger,
                "pii_masked",
                request_id=getattr(request.state, "request_id", None),
                counts=pii_summary,
            )

    provider = registry.provider_for_model(normalized.model)

    request.state.provider = provider.name

    if normalized.stream:
        stream_lease = await request.app.state.stream_limiter.acquire(principal.api_key_hash)
        if stream_lease is None:
            raise RateLimitError("Too many concurrent streams")

        deadline = time.monotonic() + settings.stream_max_duration_seconds
        upstream_stream: AsyncIterator[NormalizedStreamChunk] | None = None
        try:
            upstream_stream = provider.chat_stream(normalized)
            first_chunk = await _next_chunk_with_deadline(upstream_stream, deadline)
        except StopAsyncIteration:
            first_chunk = None
        except asyncio.TimeoutError:
            if upstream_stream is not None:
                await _close_upstream_stream(upstream_stream)
            await stream_lease.release()
            return StreamingResponse(
                _stream_timeout_only(provider, normalized, request),
                status_code=504,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        except Exception:
            if upstream_stream is not None:
                await _close_upstream_stream(upstream_stream)
            await stream_lease.release()
            raise

        return StreamingResponse(
            _stream_chunks(
                provider,
                normalized,
                request,
                first_chunk,
                upstream_stream,
                stream_lease,
                deadline,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    response = await provider.chat(normalized)
    request.state.token_usage = response.usage.model_dump()
    return ChatCompletionResponse.from_normalized(response)
