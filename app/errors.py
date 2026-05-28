from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("ai_serving.errors")


def openai_error_body(message: str, error_type: str, code: str | None) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": code}}


class APIError(Exception):
    status_code = 500
    error_type = "server_error"
    code: str | None = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code or self.status_code
        self.error_type = error_type or self.error_type
        self.code = code if code is not None else self.code
        self.headers = headers or {}


class AuthenticationError(APIError):
    status_code = 401
    error_type = "invalid_request_error"
    code = "invalid_api_key"


class RateLimitError(APIError):
    status_code = 429
    error_type = "rate_limit_error"
    code = "rate_limit_exceeded"


class PolicyViolationError(APIError):
    status_code = 403
    error_type = "content_policy_violation"
    code = "content_policy_violation"

    def __init__(self, message: str = "Request blocked by content policy.") -> None:
        super().__init__(message)


class UnsupportedModelError(APIError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "model_not_found"


class ProviderConfigurationError(APIError):
    status_code = 503
    error_type = "server_error"
    code = "provider_not_configured"


class ProviderAPIError(APIError):
    status_code = 502
    error_type = "provider_error"
    code = "provider_error"

    def __init__(
        self,
        message: str = "Upstream provider error",
        *,
        provider: str | None = None,
        upstream_status: int | None = None,
        raw_message: str | None = None,
        raw_error_class: str | None = None,
        retry_after: str | None = None,
    ) -> None:
        self.provider = provider
        self.upstream_status = upstream_status
        self.raw_message = raw_message
        self.raw_error_class = raw_error_class
        self.retry_after = retry_after

        status_code = 502
        error_type = "provider_error"
        code = "provider_error"
        public_message = "Upstream provider error"
        headers: dict[str, str] = {}

        if upstream_status == 429:
            status_code = 429
            error_type = "rate_limit_error"
            code = "rate_limit_exceeded"
            public_message = "Upstream provider rate limit exceeded"
            if retry_after:
                headers["Retry-After"] = retry_after
        elif upstream_status in {401, 403}:
            public_message = "Upstream provider error"
        elif upstream_status is not None and 400 <= upstream_status < 500:
            status_code = 400
            error_type = "invalid_request_error"
            code = "model_not_found" if upstream_status in {400, 404} else "invalid_request"
            public_message = (
                "Upstream provider rejected the requested model"
                if code == "model_not_found"
                else "Upstream provider rejected the request"
            )

        super().__init__(
            public_message,
            status_code=status_code,
            error_type=error_type,
            code=code,
            headers=headers,
        )


def _validation_message(exc: RequestValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "Invalid value")
        parts.append(f"{location}: {message}" if location else message)
    return "Invalid request: " + "; ".join(parts)


async def api_error_handler(request: Request, exc: APIError) -> JSONResponse:
    request.state.error_type = exc.error_type
    if isinstance(exc, ProviderAPIError):
        _log_provider_api_error(request, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content=openai_error_body(exc.message, exc.error_type, exc.code),
        headers=exc.headers,
    )


async def policy_violation_error_handler(
    request: Request, exc: PolicyViolationError
) -> JSONResponse:
    request.state.error_type = exc.error_type
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "param": None,
                "code": exc.code,
            }
        },
        headers=exc.headers,
    )


def _log_provider_api_error(request: Request, exc: ProviderAPIError) -> None:
    request_id = getattr(request.state, "request_id", None)
    level = (
        logging.ERROR
        if exc.upstream_status is None or exc.upstream_status >= 500
        else logging.WARNING
    )
    logger.log(
        level,
        "Upstream provider error",
        extra={
            "extra_fields": {
                "request_id": request_id,
                "provider_error_class": exc.raw_error_class or type(exc).__name__,
                "upstream_status": exc.upstream_status,
            }
        },
    )


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    request.state.error_type = "invalid_request_error"
    return JSONResponse(
        status_code=422,
        content=openai_error_body(
            _validation_message(exc),
            "invalid_request_error",
            "invalid_request",
        ),
    )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    request.state.error_type = "http_error"
    message = str(exc.detail) if exc.detail else "HTTP error"
    code = "request_too_large" if exc.status_code == 413 else "http_error"
    return JSONResponse(
        status_code=exc.status_code,
        content=openai_error_body(message, "invalid_request_error", code),
        headers=getattr(exc, "headers", None),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request.state.error_type = "server_error"
    request_id = getattr(request.state, "request_id", None)
    logger.exception("Unhandled application error", extra={"extra_fields": {"request_id": request_id}})
    return JSONResponse(
        status_code=500,
        content=openai_error_body(
            "Internal server error",
            "server_error",
            "internal_error",
        ),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(PolicyViolationError, policy_violation_error_handler)
    app.add_exception_handler(APIError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
