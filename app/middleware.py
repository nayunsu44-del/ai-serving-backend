from __future__ import annotations

import math
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import openai_error_body
from app.net import client_ip
from app.ratelimit.base import RateLimitBackend, RateLimitResult


class RequestBodyTooLarge(StarletteHTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="Request body too large")


def _error_response(
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=openai_error_body(message, error_type, code),
        headers=headers,
    )


class BodySizeLimitMiddleware:
    """Reject oversized HTTP bodies before downstream request parsing."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await _error_response(
                        413,
                        "Request body too large",
                        "invalid_request_error",
                        "request_too_large",
                    )(scope, receive, send)
                    return
            except ValueError:
                pass

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise RequestBodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestBodyTooLarge:
            await _error_response(
                413,
                "Request body too large",
                "invalid_request_error",
                "request_too_large",
            )(scope, receive, send)


class PreAuthRateLimitMiddleware(BaseHTTPMiddleware):
    """Limit failed or missing bearer auth attempts by client IP."""

    def __init__(self, app: ASGIApp, limiter: RateLimitBackend) -> None:
        super().__init__(app)
        self.limiter = limiter

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self.limiter.requests_per_minute <= 0:
            return await call_next(request)

        settings = getattr(request.app.state, "settings", None)
        key = client_ip(request, settings)
        preview = await self.limiter.preview(key)
        if not preview.allowed:
            return self._rate_limit_response(preview)

        response = await call_next(request)
        if response.status_code == 401:
            charged = await self.limiter.allow(key)
            if not charged.allowed:
                return self._rate_limit_response(charged)

        return response

    @staticmethod
    def _rate_limit_response(result: RateLimitResult) -> JSONResponse:
        retry_after = max(1, math.ceil(result.retry_after_seconds))
        return _error_response(
            429,
            "Authentication rate limit exceeded",
            "rate_limit_error",
            "rate_limit_exceeded",
            headers={"Retry-After": str(retry_after)},
        )
