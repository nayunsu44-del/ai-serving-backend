from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import pytest
from fastapi import Depends

from app.auth import require_scope
from app.db.models import APIKey, Organization
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


class ScopeProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            id="chatcmpl-scope",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="ok"),
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        raise AssertionError("not used")
        yield NormalizedStreamChunk(model=request.model)


class ScopeRegistry:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


async def _create_scoped_key(sessionmaker, plaintext: str, scopes: str) -> None:
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    async with sessionmaker() as session:
        org = Organization(name="Scope Test Org")
        session.add(org)
        await session.flush()
        session.add(
            APIKey(
                org_id=org.id,
                prefix=plaintext[:12],
                key_hash=key_hash,
                name="Scoped test key",
                scopes=scopes,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_chat_scope_allows_chat_but_not_admin_dependency(
    app,
    client,
    db_sessionmaker,
):
    plaintext = "scope-test-key"
    await _create_scoped_key(db_sessionmaker, plaintext, scopes="chat")
    app.dependency_overrides[get_provider_registry] = lambda: ScopeRegistry(ScopeProvider())

    @app.get("/test/admin")
    async def admin_test(_principal=Depends(require_scope("admin"))):
        return {"ok": True}

    headers = {"Authorization": f"Bearer {plaintext}"}
    chat_response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )
    admin_response = await client.get("/test/admin", headers=headers)

    assert chat_response.status_code == 200
    assert admin_response.status_code == 401
