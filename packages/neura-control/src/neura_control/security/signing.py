"""Signing desired-state manifests (design §17.3, §20.8). Sites verify every manifest against the
key they pinned at enrollment and reject anything unsigned or signed by another key."""

from __future__ import annotations

import base64
import hashlib
import logging

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

log = logging.getLogger("neura.control")


class ManifestSigner:
    def __init__(self, seed_b64: str | None) -> None:
        if seed_b64 is None:
            log.warning(
                "no manifest_signing_key configured: using an ephemeral key (development only)"
            )
            self._key = Ed25519PrivateKey.generate()
        else:
            seed = base64.urlsafe_b64decode(seed_b64 + "=" * (-len(seed_b64) % 4))
            if len(seed) != 32:
                raise ValueError("manifest_signing_key must be 32 bytes, base64url encoded")
            self._key = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_key = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = "ed25519:" + hashlib.sha256(self.public_key).hexdigest()[:16]

    def sign(self, payload: bytes) -> bytes:
        return self._key.sign(payload)


def new_seed() -> str:
    key = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives.serialization import NoEncryption, PrivateFormat

    raw = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
