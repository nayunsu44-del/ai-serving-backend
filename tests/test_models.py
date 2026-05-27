from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_models_returns_configured_models(client, auth_headers):
    response = await client.get("/v1/models", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": "gpt-test", "object": "model", "owned_by": "openai"},
            {"id": "claude-test", "object": "model", "owned_by": "anthropic"},
        ],
    }

