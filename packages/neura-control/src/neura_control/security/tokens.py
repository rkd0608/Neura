"""Secrets shown once: API tokens and enrollment tokens.

Format: ``<prefix>_<resource ULID>_<secret>``. Only ``sha256(secret)`` is stored; the resource id
in the token lets lookups go by primary key, and comparison is constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

API_TOKEN_PREFIX = "ntk"
ENROLLMENT_TOKEN_PREFIX = "nen"
_TOKEN_RE = re.compile(r"^(ntk|nen)_((?:tok|enr)_[0-9A-HJKMNP-TV-Z]{26})_([A-Za-z0-9_-]{43})$")


@dataclass(frozen=True, slots=True)
class ParsedToken:
    prefix: str
    resource_id: str
    secret: str


def new_secret() -> str:
    return secrets.token_urlsafe(32)  # 43 characters


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def format_token(prefix: str, resource_id: str, secret: str) -> str:
    return f"{prefix}_{resource_id}_{secret}"


def parse_token(token: str) -> ParsedToken | None:
    match = _TOKEN_RE.match(token)
    if match is None:
        return None
    return ParsedToken(match.group(1), match.group(2), match.group(3))


def verify_secret(secret: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_secret(secret), stored_hash)
