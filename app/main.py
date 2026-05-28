from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import APIKeyResolver, EnvAPIKeyStore
from app.config import Settings
from app.db.engine import create_engine, create_sessionmaker
from app.db.models import Base
from app.errors import register_exception_handlers
from app.middleware import BodySizeLimitMiddleware, PreAuthRateLimitMiddleware
from app.observability import install_request_id_middleware, setup_logging
from app.providers.registry import ProviderRegistry
from app.ratelimit import build_rate_limit_backend
from app.routers import admin, chat, health, models
from app.streaming import StreamConcurrencyLimiter


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)

    env_api_key_store = EnvAPIKeyStore.from_plaintext(settings.api_keys)
    api_key_resolver = APIKeyResolver(
        env_api_key_store,
        settings.api_key_last_used_min_interval_seconds,
    )
    settings.discard_raw_api_keys()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        Path(settings.audit_fallback_path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(settings.database_url)
        app.state.db = engine
        app.state.db_sessionmaker = create_sessionmaker(engine)
        app.state.audit_tasks = set()
        app.state.auth_tasks = set()
        app.state.api_key_resolver.auth_tasks = app.state.auth_tasks
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        try:
            yield
        finally:
            audit_tasks = list(getattr(app.state, "audit_tasks", set()))
            auth_tasks = list(getattr(app.state, "auth_tasks", set()))
            tasks = audit_tasks + auth_tasks
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            closed_limiter_ids: set[int] = set()
            for limiter_name in ("rate_limiter", "pre_auth_rate_limiter"):
                rate_limiter = getattr(app.state, limiter_name, None)
                if rate_limiter is None or id(rate_limiter) in closed_limiter_ids:
                    continue
                closed_limiter_ids.add(id(rate_limiter))
                close = getattr(rate_limiter, "aclose", None) or getattr(
                    rate_limiter,
                    "close",
                    None,
                )
                if close is not None:
                    close_result = close()
                    if inspect.isawaitable(close_result):
                        await close_result
            await engine.dispose()

    app = FastAPI(
        title="AI Serving Backend",
        version="0.1.0",
        description="OpenAI-compatible subset for chat completions.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.api_key_store = env_api_key_store
    app.state.api_key_resolver = api_key_resolver
    app.state.rate_limiter = build_rate_limit_backend(settings)
    app.state.pre_auth_rate_limiter = build_rate_limit_backend(
        settings,
        requests_per_minute=settings.pre_auth_rpm_per_ip,
        key_prefix="preauth:",
    )
    app.state.stream_limiter = StreamConcurrencyLimiter(settings.max_concurrent_streams_per_key)
    app.state.provider_registry = ProviderRegistry(settings)
    app.state.audit_tasks = set()
    app.state.auth_tasks = set()
    app.state.api_key_resolver.auth_tasks = app.state.auth_tasks

    register_exception_handlers(app)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(PreAuthRateLimitMiddleware, limiter=app.state.pre_auth_rate_limiter)
    install_request_id_middleware(app)

    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(admin.router)

    return app


app = create_app()
