from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, Response

from app.audit_fallback import write_audit_fallback
from app.db.models import AuditLog
from app.pricing import calculate_cost

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


AUDIT_PATH = "/v1/chat/completions"


def _usage_int(token_usage: Any, key: str) -> int:
    if token_usage is None:
        value = 0
    elif isinstance(token_usage, dict):
        value = token_usage.get(key)
    else:
        value = getattr(token_usage, key, 0)

    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _write_audit_fallback(settings: Any, fields: dict[str, Any]) -> None:
    logger = logging.getLogger("ai_serving.audit")
    try:
        path = Path(settings.audit_fallback_path)
        write_audit_fallback(path, fields)
        logger.warning(
            "Audit log written to fallback",
            extra={
                "extra_fields": {
                    "request_id": fields.get("request_id"),
                    "audit_fallback_path": str(path),
                }
            },
        )
    except Exception:
        logger.exception(
            "Audit fallback write failed",
            extra={"extra_fields": {"request_id": fields.get("request_id")}},
        )


async def _insert_audit_log(sessionmaker: Any, fields: dict[str, Any], settings: Any) -> None:
    logger = logging.getLogger("ai_serving.audit")
    try:
        async with sessionmaker() as session:
            session.add(AuditLog(**fields))
            await session.commit()
    except Exception:
        logger.warning(
            "Audit log insert failed; writing fallback",
            extra={"extra_fields": {"request_id": fields.get("request_id")}},
            exc_info=True,
        )
        _write_audit_fallback(settings, fields)


async def _schedule_audit_log(request: Request, status_code: int, latency_ms: int) -> None:
    settings = getattr(request.app.state, "settings", None)
    if not settings or not settings.audit_enabled or request.url.path != AUDIT_PATH:
        return

    token_usage = getattr(request.state, "token_usage", None)

    prompt_tokens = _usage_int(token_usage, "prompt_tokens")
    completion_tokens = _usage_int(token_usage, "completion_tokens")
    total_tokens = _usage_int(token_usage, "total_tokens")
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    model = getattr(request.state, "model", None)
    fields = {
        "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
        "principal_hash": getattr(request.state, "principal_hash", None),
        "org_id": getattr(request.state, "org_id", None),
        "api_key_id": getattr(request.state, "api_key_id", None),
        "provider": getattr(request.state, "provider", None),
        "model": model,
        "status_code": status_code,
        "error_type": getattr(request.state, "error_type", None),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": calculate_cost(model, prompt_tokens, completion_tokens),
        "latency_ms": latency_ms,
        "stream": bool(getattr(request.state, "stream", False)),
    }

    sessionmaker = getattr(request.app.state, "db_sessionmaker", None)
    if sessionmaker is None:
        logging.getLogger("ai_serving.audit").warning(
            "Audit DB is not initialized",
            extra={"extra_fields": {"request_id": fields.get("request_id")}},
        )
        return

    if settings.audit_sync:
        await _insert_audit_log(sessionmaker, fields, settings)
        return

    task = asyncio.create_task(_insert_audit_log(sessionmaker, fields, settings))
    tasks = getattr(request.app.state, "audit_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)


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
            latency_ms = int(round((time.perf_counter() - start) * 1000))
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
            try:
                await _schedule_audit_log(request, status_code, latency_ms)
            except Exception:
                logging.getLogger("ai_serving.audit").exception(
                    "Failed to schedule audit log",
                    extra={"extra_fields": {"request_id": request_id}},
                )
