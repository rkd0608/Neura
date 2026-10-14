"""Verifying the short-lived tokens site agents sign with their enrolled Ed25519 keys."""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUDIENCE = "neura-control"


class AgentTokenError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AgentClaims:
    server_id: str


def unverified_server_id(token: str) -> str:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise AgentTokenError(str(exc)) from exc
    subject = claims.get("sub")
    if not isinstance(subject, str):
        raise AgentTokenError("token has no subject")
    return subject


def verify(token: str, public_key_hex: str, *, max_age_s: int) -> AgentClaims:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        claims = jwt.decode(
            token,
            key,
            algorithms=["EdDSA"],
            audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "aud"]},
            leeway=30,
        )
    except (jwt.PyJWTError, ValueError) as exc:
        raise AgentTokenError(str(exc)) from exc
    if int(claims["exp"]) - int(claims["iat"]) > max_age_s:
        raise AgentTokenError("token lifetime exceeds the allowed maximum")
    return AgentClaims(server_id=str(claims["sub"]))
