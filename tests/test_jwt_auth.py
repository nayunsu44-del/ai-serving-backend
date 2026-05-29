from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

from app.auth_jwt import JWTValidator
from app.config import Settings
from app.db.models import AuditLog, Organization
from app.main import create_app
from app.normalized import (
    NormalizedChatRequest,
    NormalizedChatResponse,
    NormalizedMessage,
    NormalizedStreamChunk,
    NormalizedUsage,
)
from app.providers.base import AIProvider
from app.providers.registry import get_provider_registry
from app.schemas import ModelInfo

ISSUER = "https://issuer.example.test"
AUDIENCE = "ai-serving-backend"
KID = "test-key-1"
SUBJECT = "raw-user-secret@example.com"


class CaptureProvider(AIProvider):
    name = "fake"

    def __init__(self) -> None:
        self.last_request: NormalizedChatRequest | None = None

    async def chat(self, request: NormalizedChatRequest) -> NormalizedChatResponse:
        self.last_request = request
        return NormalizedChatResponse(
            id="chatcmpl-jwt-test",
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


class CaptureRegistry:
    def __init__(self, provider: CaptureProvider) -> None:
        self.provider = provider

    def provider_for_model(self, model: str) -> AIProvider:
        return self.provider

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="gpt-test", owned_by="fake")]


class FakeJWKClient:
    def __init__(self, public_key: bytes, kid: str = KID) -> None:
        self.public_key = public_key
        self.kid = kid

    def get_signing_key_from_jwt(self, token: str):
        header = jwt.get_unverified_header(token)
        if header.get("kid") != self.kid:
            raise RuntimeError("unknown kid")
        return SimpleNamespace(key=self.public_key)


def _generate_rsa_keypair() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


@pytest.fixture
def rsa_keypair() -> tuple[bytes, bytes]:
    return _generate_rsa_keypair()


def _settings(
    *,
    auth_mode: list[str] | None = None,
    algorithms: list[str] | None = None,
    group_scope_map: list[str] | None = None,
) -> Settings:
    return Settings(
        api_keys=["test-key"],
        auth_mode=auth_mode or ["api_key", "jwt"],
        rate_limit_rpm=1000,
        openai_models=["gpt-test"],
        anthropic_models=["claude-test"],
        database_url="sqlite+aiosqlite:///:memory:",
        audit_sync=True,
        jwt_issuer=ISSUER,
        jwt_audience=AUDIENCE,
        jwt_jwks_url="https://issuer.example.test/.well-known/jwks.json",
        jwt_algorithms=algorithms or ["RS256"],
        jwt_scope_claim="groups",
        jwt_group_scope_map=group_scope_map or ["ai-user=chat", "ai-admin=admin"],
        jwt_org_claim="org_id",
    )


def test_auth_mode_rejects_unknown_values() -> None:
    with pytest.raises(ValidationError):
        Settings(auth_mode=["api_key", "unknown"])


def _claims(
    org_id: str,
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    subject: str = SUBJECT,
    groups=None,
    expires_delta: timedelta = timedelta(minutes=5),
) -> dict:
    return {
        "iss": issuer,
        "aud": audience,
        "sub": subject,
        "exp": datetime.now(UTC) + expires_delta,
        "org_id": org_id,
        "groups": ["ai-user"] if groups is None else groups,
    }


def _encode_rs256(
    claims: dict,
    private_key: bytes,
    *,
    kid: str = KID,
) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


def _encode_hs256(claims: dict, *, kid: str = KID) -> str:
    return jwt.encode(
        claims,
        "shared-secret-for-disallowed-alg-test",
        algorithm="HS256",
        headers={"kid": kid},
    )


@asynccontextmanager
async def _test_client(settings: Settings, public_key: bytes):
    app = create_app(settings)
    app.state.jwt_validator = JWTValidator(
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        algorithms=settings.jwt_algorithms,
        jwk_client=FakeJWKClient(public_key),
    )
    provider = CaptureProvider()
    app.dependency_overrides[get_provider_registry] = lambda: CaptureRegistry(provider)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield app, client, provider


async def _create_org(sessionmaker) -> str:
    async with sessionmaker() as session:
        org = Organization(name=f"JWT Test Org {uuid.uuid4()}")
        session.add(org)
        await session.commit()
        return org.id


async def _chat(client: AsyncClient, token: str):
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "gpt-test", "messages": [{"role": "user", "content": "hello"}]},
    )


@pytest.mark.asyncio
async def test_existing_api_key_auth_still_works(rsa_keypair) -> None:
    _, public_key = rsa_keypair
    async with _test_client(_settings(), public_key) as (_app, client, _provider):
        response = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_valid_jwt_calls_provider_and_writes_hashed_audit_principal(
    rsa_keypair,
) -> None:
    private_key, public_key = rsa_keypair
    settings = _settings(group_scope_map=["ai-user=chat", "ai-admin=admin"])
    async with _test_client(settings, public_key) as (app, client, provider):
        org_id = await _create_org(app.state.db_sessionmaker)
        token = _encode_rs256(_claims(org_id, groups=["ai-user", "ai-admin"]), private_key)

        response = await _chat(client, token)

        assert response.status_code == 200
        assert provider.last_request is not None

        async with app.state.db_sessionmaker() as session:
            result = await session.execute(
                select(AuditLog).where(
                    AuditLog.request_id == response.headers["x-request-id"]
                )
            )
            audit_log = result.scalar_one()

    expected_hash = hashlib.sha256(f"jwt:{ISSUER}:{SUBJECT}".encode("utf-8")).hexdigest()
    row_text = " ".join(
        str(getattr(audit_log, column.name)) for column in AuditLog.__table__.columns
    )
    assert audit_log.org_id == org_id
    assert audit_log.api_key_id is None
    assert audit_log.principal_hash == expected_hash
    assert SUBJECT not in row_text
    assert "raw-user-secret" not in row_text


@pytest.mark.asyncio
async def test_jwt_mapped_scopes_control_chat_and_admin_access(rsa_keypair) -> None:
    private_key, public_key = rsa_keypair
    async with _test_client(_settings(), public_key) as (app, client, _provider):
        org_id = await _create_org(app.state.db_sessionmaker)
        chat_only = _encode_rs256(_claims(org_id, groups=["ai-user"]), private_key)
        chat_response = await _chat(client, chat_only)
        admin_response = await client.get(
            "/admin/usage",
            headers={"Authorization": f"Bearer {chat_only}"},
        )

        admin_and_chat = _encode_rs256(
            _claims(org_id, groups=["ai-user", "ai-admin"]),
            private_key,
        )
        admin_ok_response = await client.get(
            "/admin/usage",
            headers={"Authorization": f"Bearer {admin_and_chat}"},
        )

    assert chat_response.status_code == 200
    assert admin_response.status_code == 401
    assert admin_ok_response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "token_builder", "expected_status"),
    [
        (
            "expired",
            lambda org_id, private_key, _other_private_key: _encode_rs256(
                _claims(org_id, expires_delta=timedelta(minutes=-5)),
                private_key,
            ),
            401,
        ),
        (
            "wrong_issuer",
            lambda org_id, private_key, _other_private_key: _encode_rs256(
                _claims(org_id, issuer="https://wrong-issuer.example.test"),
                private_key,
            ),
            401,
        ),
        (
            "wrong_audience",
            lambda org_id, private_key, _other_private_key: _encode_rs256(
                _claims(org_id, audience="wrong-audience"),
                private_key,
            ),
            401,
        ),
        (
            "bad_signature",
            lambda org_id, _private_key, other_private_key: _encode_rs256(
                _claims(org_id),
                other_private_key,
            ),
            401,
        ),
        (
            "unknown_kid",
            lambda org_id, private_key, _other_private_key: _encode_rs256(
                _claims(org_id),
                private_key,
                kid="unknown-kid",
            ),
            401,
        ),
        (
            "disallowed_algorithm",
            lambda org_id, _private_key, _other_private_key: _encode_hs256(
                _claims(org_id)
            ),
            401,
        ),
        (
            "missing_org_claim",
            lambda _org_id, private_key, _other_private_key: _encode_rs256(
                {
                    key: value
                    for key, value in _claims("unused-org").items()
                    if key != "org_id"
                },
                private_key,
            ),
            401,
        ),
        (
            "unknown_org",
            lambda _org_id, private_key, _other_private_key: _encode_rs256(
                _claims(str(uuid.uuid4())),
                private_key,
            ),
            401,
        ),
        (
            "unmapped_groups",
            lambda org_id, private_key, _other_private_key: _encode_rs256(
                _claims(org_id, groups=["unmapped-group"]),
                private_key,
            ),
            401,
        ),
    ],
)
async def test_jwt_rejects_invalid_tokens_and_unmapped_principals(
    rsa_keypair,
    case,
    token_builder,
    expected_status,
) -> None:
    private_key, public_key = rsa_keypair
    other_private_key, _other_public_key = _generate_rsa_keypair()
    async with _test_client(_settings(), public_key) as (app, client, _provider):
        org_id = await _create_org(app.state.db_sessionmaker)
        token = token_builder(org_id, private_key, other_private_key)

        response = await _chat(client, token)

    assert case
    assert response.status_code == expected_status


@pytest.mark.asyncio
async def test_auth_mode_api_key_only_rejects_valid_jwt(rsa_keypair) -> None:
    private_key, public_key = rsa_keypair
    async with _test_client(
        _settings(auth_mode=["api_key"]),
        public_key,
    ) as (app, client, _provider):
        org_id = await _create_org(app.state.db_sessionmaker)
        token = _encode_rs256(_claims(org_id), private_key)

        response = await _chat(client, token)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_mode_jwt_only_rejects_valid_api_key(rsa_keypair) -> None:
    _, public_key = rsa_keypair
    async with _test_client(
        _settings(auth_mode=["jwt"]),
        public_key,
    ) as (_app, client, _provider):
        response = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key"},
        )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_mode_api_key_and_jwt_accepts_both(rsa_keypair) -> None:
    private_key, public_key = rsa_keypair
    async with _test_client(
        _settings(auth_mode=["api_key", "jwt"]),
        public_key,
    ) as (app, client, _provider):
        org_id = await _create_org(app.state.db_sessionmaker)
        token = _encode_rs256(_claims(org_id), private_key)

        api_key_response = await client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key"},
        )
        jwt_response = await _chat(client, token)

    assert api_key_response.status_code == 200
    assert jwt_response.status_code == 200
