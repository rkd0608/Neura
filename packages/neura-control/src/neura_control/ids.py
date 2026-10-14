"""Typed resource identifiers: ``<prefix>_<ULID>`` (design §20.3, api/openapi.yaml).

ULIDs sort by creation time, which makes cursor pagination a simple ``id > cursor`` scan.
"""

from __future__ import annotations

import re
import secrets
import time
from enum import StrEnum

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class Kind(StrEnum):
    ORGANIZATION = "org"
    PROJECT = "prj"
    SITE = "site"
    SERVER = "srv"
    ROBOT = "rbt"
    ROBOT_PROFILE = "rpf"
    MODEL_ARTIFACT = "art"
    EXECUTION_PROFILE = "exp"
    DEPLOYMENT = "dep"
    REVISION = "rev"
    ACTIVATION_REQUEST = "actr"
    ROLLBACK = "rbk"
    SESSION = "ses"
    FAULT = "flt"
    OPERATION = "op"
    AUDIT_RECORD = "aud"
    API_TOKEN = "tok"
    SUPPORT_GRANT = "sgr"
    ENROLLMENT_TOKEN = "enr"


ULID_PATTERN = "[0-9A-HJKMNP-TV-Z]{26}"
_ID_RE = re.compile(rf"^([a-z]+)_({ULID_PATTERN})$")


def ulid(now_ms: int | None = None) -> str:
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    value = (timestamp << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_id(kind: Kind) -> str:
    return f"{kind.value}_{ulid()}"


def kind_of(identifier: str) -> Kind | None:
    match = _ID_RE.match(identifier)
    if match is None:
        return None
    try:
        return Kind(match.group(1))
    except ValueError:
        return None


def is_id(identifier: str, kind: Kind) -> bool:
    return kind_of(identifier) is kind
