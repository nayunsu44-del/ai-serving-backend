from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.auth import APIKeyStore
from app.config import Settings
from app.main import create_app


@pytest.mark.asyncio
async def test_missing_bearer_token_returns_openai_style_error(client):
    response = await client.get("/v1/models")

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "message": "Missing bearer token",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }


@pytest.mark.asyncio
async def test_invalid_bearer_token_returns_401(client):
    response = await client.get(
        "/v1/models",
        headers={"Authorization": "Bearer wrong-key"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_valid_bearer_token_allows_request(client, auth_headers):
    response = await client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200


def test_api_keys_are_hashed_and_discarded_after_app_creation():
    settings = Settings(api_keys=["secret-key"], rate_limit_rpm=1000)
    app = create_app(settings)

    store: APIKeyStore = app.state.api_key_store
    assert len(store) == 1
    assert app.state.settings.api_keys == []
    assert "secret-key" not in repr(store.__dict__)


def test_negative_rate_limit_rpm_is_rejected():
    with pytest.raises(ValidationError):
        Settings(api_keys=["secret-key"], rate_limit_rpm=-1)


@pytest.mark.asyncio
async def test_rate_limit_returns_429(auth_headers):
    from httpx import ASGITransport, AsyncClient

    app = create_app(Settings(api_keys=["test-key"], rate_limit_rpm=1))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        first = await client.get("/v1/models", headers=auth_headers)
        second = await client.get("/v1/models", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    assert second.json()["error"]["code"] == "rate_limit_exceeded"


@pytest.mark.asyncio
async def test_zero_rate_limit_rpm_disables_rate_limit(auth_headers):
    from httpx import ASGITransport, AsyncClient

    app = create_app(Settings(api_keys=["test-key"], rate_limit_rpm=0))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        responses = [
            await client.get("/v1/models", headers=auth_headers),
            await client.get("/v1/models", headers=auth_headers),
        ]

    assert [response.status_code for response in responses] == [200, 200]
