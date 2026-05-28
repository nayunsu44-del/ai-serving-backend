from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.db.engine import get_sessionmaker
from app.db.models import APIKey, utc_now
from app.errors import AuthenticationError

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("ai_serving.auth")


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class APIKeyPrincipal:
    api_key_hash: str
    api_key_id: str | None = None
    org_id: str | None = None
    scopes: frozenset[str] = frozenset({"chat"})


class EnvAPIKeyStore:
    """Stores only SHA-256 hashes of environment-provided service API keys."""

    def __init__(self, key_hashes: list[str]) -> None:
        self._key_hashes = tuple(key_hashes)

    @classmethod
    def from_plaintext(cls, api_keys: list[str]) -> "EnvAPIKeyStore":
        return cls([_sha256_hex(key) for key in api_keys if key])

    def validate(self, token: str) -> APIKeyPrincipal | None:
        token_hash = _sha256_hex(token)
        matched_hash: str | None = None
        for stored_hash in self._key_hashes:
            if hmac.compare_digest(token_hash, stored_hash):
                matched_hash = stored_hash
        if matched_hash is None:
            return None
        return APIKeyPrincipal(
            api_key_hash=matched_hash,
            scopes=frozenset({"chat", "admin", "super_admin"}),
        )

    def __len__(self) -> int:
        return len(self._key_hashes)


APIKeyStore = EnvAPIKeyStore


def _parse_scopes(value: str | None) -> frozenset[str]:
    scopes = frozenset(item.strip() for item in (value or "chat").split(",") if item.strip())
    return scopes or frozenset({"chat"})


class APIKeyResolver:
    def __init__(self, env_store: EnvAPIKeyStore) -> None:
        self.env_store = env_store
        self.auth_tasks: set[asyncio.Task] | None = None

    async def resolve(self, token: str, sessionmaker) -> APIKeyPrincipal | None:
        token_hash = _sha256_hex(token)

        if sessionmaker is not None:
            try:
                async with sessionmaker() as session:
                    result = await session.execute(
                        select(APIKey).where(
                            APIKey.key_hash == token_hash,
                            APIKey.revoked_at.is_(None),
                        )
                    )
                    api_key = result.scalar_one_or_none()
                    if api_key is not None:
                        api_key.last_used_at = utc_now()
                        await session.commit()
                        return APIKeyPrincipal(
                            api_key_hash=token_hash,
                            api_key_id=api_key.id,
                            org_id=api_key.org_id,
                            scopes=_parse_scopes(api_key.scopes),
                        )
            except Exception:
                logger.exception("API key DB lookup failed")

        return self.env_store.validate(token)


def get_api_key_store(request: Request) -> EnvAPIKeyStore:
    return request.app.state.api_key_store


def get_api_key_resolver(request: Request) -> APIKeyResolver:
    return request.app.state.api_key_resolver


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    resolver: APIKeyResolver = Depends(get_api_key_resolver),
    sessionmaker=Depends(get_sessionmaker),
) -> APIKeyPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token")

    principal = await resolver.resolve(credentials.credentials, sessionmaker)
    if principal is None:
        raise AuthenticationError("Invalid API key")

    request.state.principal_hash = principal.api_key_hash
    request.state.api_key_id = principal.api_key_id
    request.state.org_id = principal.org_id
    request.state.scopes = principal.scopes
    return principal


def require_scope(scope: str):
    async def dependency(
        principal: APIKeyPrincipal = Depends(require_api_key),
    ) -> APIKeyPrincipal:
        if scope not in principal.scopes:
            raise AuthenticationError("Missing required scope")
        return principal

    return dependency


async def require_admin_principal(
    principal: APIKeyPrincipal = Depends(require_api_key),
) -> APIKeyPrincipal:
    if "admin" not in principal.scopes:
        raise AuthenticationError("Missing required scope")
    return principal


def is_super_admin(principal: APIKeyPrincipal) -> bool:
    return "super_admin" in principal.scopes
