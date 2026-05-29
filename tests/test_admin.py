from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.pricing import calculate_cost
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


class AdminProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            id="chatcmpl-admin",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="ok"),
            finish_reason="stop",
            usage=NormalizedUsage(
                prompt_tokens=1_000_000,
                completion_tokens=2_000_000,
                total_tokens=3_000_000,
            ),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        raise AssertionError("not used")
        yield NormalizedStreamChunk(model=request.model)


class AdminRegistry:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


async def _drain_background_tasks(app) -> None:
    tasks = tuple(getattr(app.state, "audit_tasks", set())) + tuple(
        getattr(app.state, "auth_tasks", set())
    )
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _six_dp(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001')):.6f}"


@pytest.mark.asyncio
async def test_admin_org_key_usage_audit_and_revoke_flow(
    app,
    client,
    auth_headers,
):
    org_response = await client.post(
        "/admin/orgs",
        headers=auth_headers,
        json={"name": "Admin Test Org"},
    )
    assert org_response.status_code == 200
    org = org_response.json()

    duplicate_response = await client.post(
        "/admin/orgs",
        headers=auth_headers,
        json={"name": "Admin Test Org"},
    )
    assert duplicate_response.status_code == 409

    key_response = await client.post(
        "/admin/keys",
        headers=auth_headers,
        json={"name": "Chat key", "scopes": ["chat"], "org_id": org["id"]},
    )
    assert key_response.status_code == 200
    created_key = key_response.json()
    plaintext = created_key["api_key"]
    assert plaintext.startswith("sk-")
    assert created_key["prefix"] == plaintext[:12]
    assert created_key["scopes"] == ["chat"]

    list_response = await client.get(
        f"/admin/keys?org_id={org['id']}",
        headers=auth_headers,
    )
    assert list_response.status_code == 200
    listed_keys = list_response.json()["items"]
    assert len(listed_keys) == 1
    assert listed_keys[0]["id"] == created_key["id"]
    assert "api_key" not in listed_keys[0]

    non_admin_response = await client.get(
        "/admin/keys",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert non_admin_response.status_code == 401

    app.dependency_overrides[get_provider_registry] = lambda: AdminRegistry(AdminProvider())
    chat_response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "usage please"}],
        },
    )
    assert chat_response.status_code == 200
    await _drain_background_tasks(app)

    usage_response = await client.get(
        "/admin/usage?group_by=key",
        headers=auth_headers,
    )
    assert usage_response.status_code == 200
    usage = usage_response.json()
    key_groups = {
        group["group_key"]: group
        for group in usage["groups"]
    }
    usage_group = key_groups[created_key["id"]]
    assert usage_group["request_count"] == 1
    assert usage_group["error_count"] == 0
    assert usage_group["total_prompt_tokens"] == 1_000_000
    assert usage_group["total_completion_tokens"] == 2_000_000
    assert usage_group["total_tokens"] == 3_000_000
    assert usage_group["cost_usd"] == _six_dp(
        calculate_cost("gpt-4o-mini", 1_000_000, 2_000_000)
    )

    audit_response = await client.get(
        "/admin/audit?limit=1",
        headers=auth_headers,
    )
    assert audit_response.status_code == 200
    audit_body = audit_response.json()
    assert audit_body["next_offset"] is None
    assert len(audit_body["items"]) == 1
    assert audit_body["items"][0]["request_id"] == chat_response.headers["x-request-id"]
    assert audit_body["items"][0]["api_key_id"] == created_key["id"]
    assert audit_body["items"][0]["cost_usd"] == usage_group["cost_usd"]

    revoke_response = await client.post(
        f"/admin/keys/{created_key['id']}/revoke",
        headers=auth_headers,
    )
    assert revoke_response.status_code == 200
    assert revoke_response.json()["id"] == created_key["id"]
    assert revoke_response.json()["revoked_at"] is not None

    revoked_list_response = await client.get(
        f"/admin/keys?org_id={org['id']}&include_revoked=true",
        headers=auth_headers,
    )
    assert revoked_list_response.status_code == 200
    revoked_key = revoked_list_response.json()["items"][0]
    assert revoked_key["id"] == created_key["id"]
    assert revoked_key["revoked_at"] is not None
