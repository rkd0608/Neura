"""OIDC access-token verification for people signing in through customer SSO (design §20.7)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import jwt

ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]

KeyResolver = Callable[[str], Any]


class TokenInvalidError(Exception):
    pass


class OidcVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        key_resolver: KeyResolver | None = None,
        leeway_s: int = 30,
    ) -> None:
        if key_resolver is None:
            if jwks_url is None:
                raise ValueError("an OIDC verifier needs a jwks_url or a key resolver")
            client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._issuer = issuer
        self._audience = audience
        self._resolve = key_resolver
        self._leeway = leeway_s

    def subject(self, token: str) -> str:
        """Verify signature, issuer, audience and expiry; return ``"<issuer>|<sub>"``."""
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALLOWED_ALGORITHMS:
                raise TokenInvalidError("unsupported signing algorithm")
            claims = jwt.decode(
                token,
                self._resolve(token),
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise TokenInvalidError(str(exc)) from exc
        return f"{claims['iss']}|{claims['sub']}"
