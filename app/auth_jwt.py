from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import jwt
from jwt import PyJWKClient

logger = logging.getLogger("ai_serving.auth.jwt")


class JWTValidator:
    def __init__(
        self,
        *,
        issuer: str | None,
        audience: str | None,
        algorithms: list[str],
        jwk_client: Any,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.algorithms = algorithms
        self.jwk_client = jwk_client

    def validate(self, token: str) -> dict:
        signing_key = self.jwk_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=self.algorithms,
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iss", "aud"]},
        )


def build_jwt_validator(settings) -> JWTValidator | None:
    if "jwt" not in settings.auth_mode or not settings.jwt_jwks_url:
        return None
    return JWTValidator(
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        algorithms=settings.jwt_algorithms,
        jwk_client=PyJWKClient(settings.jwt_jwks_url),
    )


def map_groups_to_scopes(
    groups: Iterable[str],
    group_scope_map: dict[str, str],
) -> frozenset[str]:
    return frozenset(
        scope
        for group in groups
        if (scope := group_scope_map.get(group)) is not None
    )
