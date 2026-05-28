from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from datetime import timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.compliance.filter import (
    MAX_RULE_PATTERN_CHARS,
    MAX_RULES,
    _compile_rules_cached,
    compile_rules,
)
from app.db.engine import create_engine
from app.db.models import APIKey, Organization, utc_now
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.pricing import _WARNED_UNKNOWN_MODELS, calculate_cost
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry


class HardeningProvider(AIProvider):
    name = "fake"

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        return NormalizedChatResponse(
            id="chatcmpl-hardening",
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


class HardeningRegistry:
    def __init__(self) -> None:
        self.provider = HardeningProvider()

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider


async def _create_api_key(sessionmaker, plaintext: str) -> str:
    key_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    async with sessionmaker() as session:
        org = Organization(name=f"Hardening {plaintext}")
        session.add(org)
        await session.flush()

        api_key = APIKey(
            org_id=org.id,
            prefix=plaintext[:12],
            key_hash=key_hash,
            name="Hardening key",
        )
        session.add(api_key)
        await session.commit()
        return api_key.id


async def _set_last_used_at(sessionmaker, api_key_id: str, value) -> None:
    async with sessionmaker() as session:
        api_key = await session.get(APIKey, api_key_id)
        assert api_key is not None
        api_key.last_used_at = value
        await session.commit()


async def _get_last_used_at(sessionmaker, api_key_id: str):
    async with sessionmaker() as session:
        result = await session.execute(select(APIKey).where(APIKey.id == api_key_id))
        return result.scalar_one().last_used_at


async def _post_chat(client, plaintext: str):
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {plaintext}"},
        json={
            "model": "gpt-test",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )


def test_compile_rules_skips_overlong_patterns_and_caps_rule_count() -> None:
    _compile_rules_cached.cache_clear()
    overlong_regex = "a" * (MAX_RULE_PATTERN_CHARS + 1)

    assert compile_rules([f"too_long={overlong_regex}"]) == ()

    many_patterns = [f"rule_{index}=value_{index}" for index in range(MAX_RULES + 10)]
    rules = compile_rules(many_patterns)

    assert len(rules) == MAX_RULES
    assert rules[-1].rule_id == f"rule_{MAX_RULES - 1}"


def test_compile_rules_returns_cached_tuple_by_pattern_tuple() -> None:
    _compile_rules_cached.cache_clear()

    first = compile_rules(["alpha=secret"])
    second = compile_rules(["alpha=secret"])
    different = compile_rules(["beta=secret"])

    assert first is second
    assert first is not different


@pytest.mark.asyncio
async def test_db_api_key_first_use_sets_last_used_at(app, client, db_sessionmaker) -> None:
    plaintext = "db-hardening-first"
    api_key_id = await _create_api_key(db_sessionmaker, plaintext)
    app.dependency_overrides[get_provider_registry] = HardeningRegistry

    response = await _post_chat(client, plaintext)

    assert response.status_code == 200
    assert await _get_last_used_at(db_sessionmaker, api_key_id) is not None


@pytest.mark.asyncio
async def test_db_api_key_recent_naive_last_used_at_is_not_updated(
    app,
    client,
    db_sessionmaker,
) -> None:
    plaintext = "db-hardening-recent"
    api_key_id = await _create_api_key(db_sessionmaker, plaintext)
    recent = utc_now().replace(tzinfo=None)
    await _set_last_used_at(db_sessionmaker, api_key_id, recent)
    app.state.api_key_resolver.api_key_last_used_min_interval_seconds = 3600
    app.dependency_overrides[get_provider_registry] = HardeningRegistry

    response = await _post_chat(client, plaintext)

    assert response.status_code == 200
    assert await _get_last_used_at(db_sessionmaker, api_key_id) == recent


@pytest.mark.asyncio
async def test_db_api_key_interval_zero_updates_last_used_at(
    app,
    client,
    db_sessionmaker,
) -> None:
    plaintext = "db-hardening-zero"
    api_key_id = await _create_api_key(db_sessionmaker, plaintext)
    old = (utc_now() - timedelta(hours=1)).replace(tzinfo=None)
    await _set_last_used_at(db_sessionmaker, api_key_id, old)
    app.state.api_key_resolver.api_key_last_used_min_interval_seconds = 0
    app.dependency_overrides[get_provider_registry] = HardeningRegistry

    response = await _post_chat(client, plaintext)
    updated = await _get_last_used_at(db_sessionmaker, api_key_id)

    assert response.status_code == 200
    assert updated is not None
    assert updated.replace(tzinfo=timezone.utc) > old.replace(tzinfo=timezone.utc)


def test_unknown_model_pricing_warns_once_per_model(caplog) -> None:
    _WARNED_UNKNOWN_MODELS.clear()
    caplog.set_level(logging.WARNING, logger="ai_serving.pricing")

    first = calculate_cost("unknown-hardening-model", 10, 20)
    second = calculate_cost("unknown-hardening-model", 30, 40)

    pricing_warnings = [
        record
        for record in caplog.records
        if record.name == "ai_serving.pricing"
        and "Unknown model for pricing" in record.getMessage()
    ]
    assert first == Decimal("0")
    assert second == Decimal("0")
    assert len(pricing_warnings) == 1


@pytest.mark.asyncio
async def test_file_sqlite_engine_enables_wal_and_memory_engine_still_connects(tmp_path) -> None:
    db_path = tmp_path / "hardening.db"
    file_engine = create_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    try:
        async with file_engine.connect() as conn:
            journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar_one()
        assert journal_mode == "wal"
    finally:
        await file_engine.dispose()

    memory_engine = create_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with memory_engine.connect() as conn:
            result = (await conn.exec_driver_sql("SELECT 1")).scalar_one()
        assert result == 1
    finally:
        await memory_engine.dispose()
