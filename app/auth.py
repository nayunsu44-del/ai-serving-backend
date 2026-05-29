from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.auth_jwt import map_groups_to_scopes
from app.config import parse_jwt_group_scope_map
from app.db.engine import get_sessionmaker
from app.db.models import APIKey, Organization, utc_now
from app.errors import AuthenticationError

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("ai_serving.auth")
jwt_logger = logging.getLogger("ai_serving.auth.jwt")


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
    def __init__(
        self,
        env_store: EnvAPIKeyStore,
        api_key_last_used_min_interval_seconds: int = 60,
    ) -> None:
        self.env_store = env_store
        self.api_key_last_used_min_interval_seconds = (
            api_key_last_used_min_interval_seconds
        )
        self.auth_tasks: set[asyncio.Task] | None = None

    def _should_update_last_used(self, last_used_at: datetime | None, now: datetime) -> bool:
        interval = self.api_key_last_used_min_interval_seconds
        if interval == 0 or last_used_at is None:
            return True
        if last_used_at.tzinfo is None:
            last_used_at = last_used_at.replace(tzinfo=timezone.utc)
        try:
            return last_used_at <= now - timedelta(seconds=interval)
        except TypeError:
            return True

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
                        now = utc_now()
                        if self._should_update_last_used(api_key.last_used_at, now):
                            api_key.last_used_at = now
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


def _set_request_principal(request: Request, principal: APIKeyPrincipal) -> None:
    request.state.principal_hash = principal.api_key_hash
    request.state.api_key_id = principal.api_key_id
    request.state.org_id = principal.org_id
    request.state.scopes = principal.scopes


def _groups_from_claim(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


async def _build_jwt_principal(request: Request, claims: dict, sessionmaker) -> APIKeyPrincipal:
    settings = request.app.state.settings
    iss = claims.get("iss")
    sub = claims.get("sub")
    if not iss or not sub:
        raise AuthenticationError("Invalid credentials")

    org_value = claims.get(settings.jwt_org_claim)
    if org_value is None:
        raise AuthenticationError("Invalid credentials")
    if sessionmaker is None:
        raise AuthenticationError("Invalid credentials")

    async with sessionmaker() as session:
        result = await session.execute(
            select(Organization).where(Organization.id == str(org_value))
        )
        org = result.scalar_one_or_none()
    if org is None:
        raise AuthenticationError("Invalid credentials")

    group_scope_map = parse_jwt_group_scope_map(settings.jwt_group_scope_map)
    groups = _groups_from_claim(claims.get(settings.jwt_scope_claim))
    scopes = map_groups_to_scopes(groups, group_scope_map)
    if not scopes:
        raise AuthenticationError("Invalid credentials")

    return APIKeyPrincipal(
        api_key_hash=_sha256_hex(f"jwt:{iss}:{sub}"),
        api_key_id=None,
        org_id=org.id,
        scopes=scopes,
    )


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    resolver: APIKeyResolver = Depends(get_api_key_resolver),
    sessionmaker=Depends(get_sessionmaker),
) -> APIKeyPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token")

    token = credentials.credentials
    settings = request.app.state.settings
    modes = settings.auth_mode

    if "api_key" in modes:
        principal = await resolver.resolve(token, sessionmaker)
        if principal is not None:
            _set_request_principal(request, principal)
            return principal

    if "jwt" in modes:
        validator = getattr(request.app.state, "jwt_validator", None)
        if validator is not None:
            try:
                claims = validator.validate(token)
            except jwt.PyJWTError as exc:
                jwt_logger.warning(
                    "JWT validation failed",
                    extra={"extra_fields": {"error_class": type(exc).__name__}},
                )
            except Exception as exc:
                jwt_logger.warning(
                    "JWT authentication failed",
                    extra={"extra_fields": {"error_class": type(exc).__name__}},
                )
            else:
                principal = await _build_jwt_principal(request, claims, sessionmaker)
                _set_request_principal(request, principal)
                return principal

    raise AuthenticationError("Invalid credentials")


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
