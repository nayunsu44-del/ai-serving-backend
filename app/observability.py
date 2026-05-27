from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response

SECRET_FIELD_RE = re.compile(r"(api[_-]?key|authorization|token|secret|\bkey\b)", re.IGNORECASE)


def _sanitize_extra_fields(fields: dict[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or SECRET_FIELD_RE.search(key):
            continue
        if isinstance(value, dict):
            sanitized[key] = _sanitize_extra_fields(value)
        else:
            sanitized[key] = value
    return sanitized


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(_sanitize_extra_fields(extra_fields))
        if record.exc_info:
            payload["exception_class"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    logging.getLogger("httpx").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(event, extra={"extra_fields": fields})


def install_request_id_middleware(app: FastAPI) -> None:
    logger = logging.getLogger("ai_serving.requests")

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.perf_counter()
        status_code = 500
        response: Response | None = None

        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            if response is not None:
                response.headers["x-request-id"] = request_id

            log_event(
                logger,
                "request_complete",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=status_code,
                latency_ms=latency_ms,
                provider=getattr(request.state, "provider", None),
                model=getattr(request.state, "model", None),
                token_usage=getattr(request.state, "token_usage", None),
                error_type=getattr(request.state, "error_type", None),
            )
