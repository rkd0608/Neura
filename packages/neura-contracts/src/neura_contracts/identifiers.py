"""ULIDs: 128-bit identifiers that sort by creation time, in Crockford base32 (26 characters).

Shared by every component that mints resource identifiers, so ids created at a site (such as robot
sessions) and in the control plane have one format: ``<prefix>_<ULID>``.
"""

from __future__ import annotations

import secrets
import time

CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_PATTERN = "[0-9A-HJKMNP-TV-Z]{26}"


def ulid(now_ms: int | None = None) -> str:
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    value = (timestamp << 80) | secrets.randbits(80)
    chars = []
    for _ in range(26):
        chars.append(CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))
