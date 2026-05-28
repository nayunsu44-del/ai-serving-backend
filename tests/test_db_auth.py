from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select

from app.db.models import APIKey, AuditLog, Organization, utc_now
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


class DBAuthProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            id="chatcmpl-db-auth",
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


class DBAuthRegistry:
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


async def _create_api_key(sessionmaker, plaintext: str) -> tuple[str, str]:
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    async with sessionmaker() as session:
        org = Organization(name="DB Auth Test Org")
        session.add(org)
        await session.flush()

        api_key = APIKey(
            org_id=org.id,
            prefix=plaintext[:12],
            key_hash=key_hash,
            name="DB test key",
        )
        session.add(api_key)
        await session.commit()
        return org.id, api_key.id


@pytest.mark.asyncio
async def test_db_api_key_auth_writes_audit_identity_and_last_used(
    app,
    client,
    db_sessionmaker,
):
    plaintext = "db-test-key"
    org_id, api_key_id = await _create_api_key(db_sessionmaker, plaintext)
    provider = DBAuthProvider()
    app.dependency_overrides[get_provider_registry] = lambda: DBAuthRegistry(provider)

    response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    assert response.status_code == 200
    await _drain_background_tasks(app)

    async with db_sessionmaker() as session:
        audit_result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == response.headers["x-request-id"])
        )
        audit_log = audit_result.scalar_one()

        key_result = await session.execute(select(APIKey).where(APIKey.id == api_key_id))
        db_key = key_result.scalar_one()

    assert audit_log.org_id == org_id
    assert audit_log.api_key_id == api_key_id
    assert db_key.last_used_at is not None

    async with db_sessionmaker() as session:
        key_result = await session.execute(select(APIKey).where(APIKey.id == api_key_id))
        db_key = key_result.scalar_one()
        db_key.revoked_at = utc_now()
        await session.commit()

    revoked_response = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello again"}],
        },
    )

    assert revoked_response.status_code == 401
