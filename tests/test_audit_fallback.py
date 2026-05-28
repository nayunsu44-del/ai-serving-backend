from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.audit_fallback import write_audit_fallback
from app.config import Settings
from app.db.models import AuditLog
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry
from app.routers import admin as admin_router


class FallbackProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            id="chatcmpl-audit-fallback",
            model=request.model,
            message=NormalizedMessage(role="assistant", content="ok"),
            finish_reason="stop",
            usage=NormalizedUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        )

    async def chat_stream(
        self, request: NormalizedChatRequest
    ) -> AsyncIterator[NormalizedStreamChunk]:
        raise AssertionError("not used")
        yield NormalizedStreamChunk(model=request.model)


class FallbackRegistry:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


class BrokenSessionmaker:
    def __call__(self):
        raise RuntimeError("database unavailable")


def _fallback_fields(request_id: str) -> dict:
    return {
        "request_id": request_id,
        "principal_hash": "abc123",
        "org_id": None,
        "api_key_id": None,
        "provider": "fake",
        "model": "gpt-test",
        "status_code": 200,
        "error_type": None,
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
        "cost_usd": str(Decimal("0.000003")),
        "latency_ms": 15,
        "stream": False,
    }


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_keys=["test-key"],
        rate_limit_rpm=1000,
        openai_models=["gpt-test"],
        anthropic_models=["claude-test"],
        database_url="sqlite+aiosqlite:///:memory:",
        audit_sync=True,
        audit_fallback_path=str(tmp_path / "audit_fallback.jsonl"),
    )


@pytest.mark.asyncio
async def test_audit_insert_failure_writes_jsonl_fallback(
    app,
    client,
    auth_headers,
    settings,
    monkeypatch,
) -> None:
    app.dependency_overrides[get_provider_registry] = lambda: FallbackRegistry(FallbackProvider())
    monkeypatch.setattr(app.state, "db_sessionmaker", BrokenSessionmaker())

    response = await client.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "audit fallback"}],
        },
    )

    assert response.status_code == 200
    lines = settings.audit_fallback_path
    with open(lines, encoding="utf-8") as handle:
        payload = json.loads(handle.readline())

    assert payload["request_id"] == response.headers["x-request-id"]
    assert payload["status_code"] == 200
    assert payload["model"] == "gpt-test"
    assert payload["prompt_tokens"] == 10
    assert payload["completion_tokens"] == 20
    assert payload["total_tokens"] == 30
    assert payload["stream"] is False


@pytest.mark.asyncio
async def test_super_admin_can_replay_audit_fallback(
    client,
    auth_headers,
    db_sessionmaker,
    settings,
) -> None:
    fields = _fallback_fields("fallback-replay-request")
    with open(settings.audit_fallback_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(fields) + "\n")

    response = await client.post("/admin/audit/replay", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"replayed": 1, "failed": 0}

    async with db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == "fallback-replay-request")
        )
        audit_log = result.scalar_one()

    assert audit_log.model == "gpt-test"
    assert audit_log.total_tokens == 3
    assert audit_log.cost_usd == Decimal("0.000003")


@pytest.mark.asyncio
async def test_replay_quarantines_failed_lines_and_preserves_concurrent_appends(
    client,
    auth_headers,
    db_sessionmaker,
    settings,
    monkeypatch,
) -> None:
    path = Path(settings.audit_fallback_path)
    good_fields = _fallback_fields("fallback-replay-good")
    concurrent_fields = _fallback_fields("fallback-concurrent-append")
    malformed_line = "{not valid json"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(good_fields) + "\n")
        handle.write(malformed_line + "\n")

    original_replace = admin_router.os.replace

    def replace_and_append(src, dst):
        original_replace(src, dst)
        write_audit_fallback(Path(src), concurrent_fields)

    monkeypatch.setattr(admin_router.os, "replace", replace_and_append)

    response = await client.post("/admin/audit/replay", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"replayed": 1, "failed": 1}

    async with db_sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.request_id == "fallback-replay-good")
        )
        audit_log = result.scalar_one()

    assert audit_log.model == "gpt-test"
    assert audit_log.total_tokens == 3

    failed_path = path.with_name(path.name + ".failed")
    assert failed_path.read_text(encoding="utf-8").splitlines() == [malformed_line]

    live_lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["request_id"] for line in live_lines] == [
        "fallback-concurrent-append"
    ]
