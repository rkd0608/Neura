"""OIDC access-token verification for people signing in through customer SSO (design §20.7)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt

ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256"]

KeyResolver = Callable[[str], Any]


class TokenInvalidError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    subject: str  # "<issuer>|<sub>"
    email: str | None  # lower case, only when the issuer marked it verified


class OidcVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        key_resolver: KeyResolver | None = None,
        leeway_s: int = 30,
        allowed_clients: frozenset[str] = frozenset(),
        required_amr: frozenset[str] = frozenset(),
    ) -> None:
        """``allowed_clients``: when set, a token issued to any other client (its ``azp``, or
        ``client_id``) is refused, so a token minted for another application of the same
        identity provider cannot be replayed here. ``required_amr``: when set, the token's
        authentication methods (``amr``) must include one of them, e.g. ``mfa``, to enforce
        multi-factor sign-in here as well as at the identity provider."""
        if key_resolver is None:
            if jwks_url is None:
                raise ValueError("an OIDC verifier needs a jwks_url or a key resolver")
            client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
            key_resolver = lambda token: client.get_signing_key_from_jwt(token).key  # noqa: E731
        self._issuer = issuer
        self._audience = audience
        self._resolve = key_resolver
        self._leeway = leeway_s
        self._clients = allowed_clients
        self._amr = required_amr

    def subject(self, token: str) -> str:
        """Verify signature, issuer, audience and expiry; return ``"<issuer>|<sub>"``."""
        return self.identity(token).subject

    def identity(self, token: str) -> OidcIdentity:
        """Verify the token and return who it identifies. The email is used only to accept an
        invitation, and only when the issuer says it is verified."""
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
        client = claims.get("azp", claims.get("client_id"))
        if self._clients and client not in self._clients:
            raise TokenInvalidError("the token was issued to another client")
        methods = claims.get("amr")
        if self._amr and not (isinstance(methods, list) and self._amr.intersection(methods)):
            raise TokenInvalidError(f"sign-in must use one of: {', '.join(sorted(self._amr))}")
        email = claims.get("email")
        verified = claims.get("email_verified") is True
        return OidcIdentity(
            subject=f"{claims['iss']}|{claims['sub']}",
            email=email.strip().lower() if isinstance(email, str) and verified else None,
        )
