"""Typed resource identifiers: ``<prefix>_<ULID>`` (design §20.3, api/openapi.yaml).

ULIDs sort by creation time, which makes cursor pagination a simple ``id > cursor`` scan.
"""

from __future__ import annotations

import re
from enum import StrEnum

from neura_contracts.identifiers import ULID_PATTERN, ulid


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
    MEMBER = "mbr"
    ENROLLMENT_TOKEN = "enr"
    EVALUATION = "evl"
    TRAINING_JOB = "trn"


_ID_RE = re.compile(rf"^([a-z]+)_({ULID_PATTERN})$")


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
