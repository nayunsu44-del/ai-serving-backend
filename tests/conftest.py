from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_keys=["test-key"],
        rate_limit_rpm=1000,
        openai_models=["gpt-test"],
        anthropic_models=["claude-test"],
        database_url="sqlite+aiosqlite:///:memory:",
        audit_sync=True,
    )


@pytest_asyncio.fixture
async def app(settings: Settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest_asyncio.fixture
async def db_sessionmaker(app):
    return app.state.db_sessionmaker


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-key"}


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
