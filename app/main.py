from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.auth import APIKeyStore
from app.config import Settings
from app.errors import register_exception_handlers
from app.middleware import BodySizeLimitMiddleware, PreAuthRateLimitMiddleware
from app.observability import install_request_id_middleware, setup_logging
from app.providers.registry import ProviderRegistry
from app.ratelimit import build_rate_limit_backend
from app.ratelimit.memory import InMemoryRateLimit
from app.routers import chat, health, models
from app.streaming import StreamConcurrencyLimiter


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level)

    api_key_store = APIKeyStore.from_plaintext(settings.api_keys)
    settings.discard_raw_api_keys()

    app = FastAPI(
        title="AI Serving Backend",
        version="0.1.0",
        description="OpenAI-compatible subset for chat completions.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings
    app.state.api_key_store = api_key_store
    app.state.rate_limiter = build_rate_limit_backend(settings)
    app.state.pre_auth_rate_limiter = InMemoryRateLimit(settings.pre_auth_rpm_per_ip)
    app.state.stream_limiter = StreamConcurrencyLimiter(settings.max_concurrent_streams_per_key)
    app.state.provider_registry = ProviderRegistry(settings)

    register_exception_handlers(app)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(PreAuthRateLimitMiddleware, limiter=app.state.pre_auth_rate_limiter)
    install_request_id_middleware(app)

    app.include_router(health.router)
    app.include_router(models.router)
    app.include_router(chat.router)

    return app


app = create_app()
