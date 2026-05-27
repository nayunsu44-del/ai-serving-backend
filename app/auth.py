from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.errors import AuthenticationError

bearer_scheme = HTTPBearer(auto_error=False)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class APIKeyPrincipal:
    api_key_hash: str


class APIKeyStore:
    """Stores only SHA-256 hashes of service API keys."""

    def __init__(self, key_hashes: list[str]) -> None:
        self._key_hashes = tuple(key_hashes)

    @classmethod
    def from_plaintext(cls, api_keys: list[str]) -> "APIKeyStore":
        return cls([_sha256_hex(key) for key in api_keys if key])

    def validate(self, token: str) -> APIKeyPrincipal | None:
        token_hash = _sha256_hex(token)
        matched_hash: str | None = None
        for stored_hash in self._key_hashes:
            if hmac.compare_digest(token_hash, stored_hash):
                matched_hash = stored_hash
        if matched_hash is None:
            return None
        return APIKeyPrincipal(api_key_hash=matched_hash)

    def __len__(self) -> int:
        return len(self._key_hashes)


def get_api_key_store(request: Request) -> APIKeyStore:
    return request.app.state.api_key_store


async def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    store: APIKeyStore = Depends(get_api_key_store),
) -> APIKeyPrincipal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Missing bearer token")

    principal = store.validate(credentials.credentials)
    if principal is None:
        raise AuthenticationError("Invalid API key")

    return principal

