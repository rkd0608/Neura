"""The agent's on-host identity: an Ed25519 key generated here, and what enrollment returned.

The private key never leaves the server (design §17.2). Each connection presents a short-lived token
signed with it; the control plane verifies it against the public key registered at enrollment.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

AUDIENCE = "neura-control"
TOKEN_LIFETIME_S = 240


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write via a temporary file, fsync and rename: readers see the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True, slots=True)
class Identity:
    server_id: str
    site_id: str
    project_id: str
    endpoint: str  # the control plane's agent endpoint, host:port
    manifest_public_key_hex: str
    manifest_key_id: str


class IdentityStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.key_path = state_dir / "agent.key"
        self.identity_path = state_dir / "identity.json"

    def load_or_create_key(self) -> Ed25519PrivateKey:
        if self.key_path.exists():
            key = load_pem_private_key(self.key_path.read_bytes(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise ValueError(f"{self.key_path} is not an Ed25519 key")
            return key
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
        atomic_write(self.key_path, pem, 0o600)
        return key

    @staticmethod
    def public_key_raw(key: Ed25519PrivateKey) -> bytes:
        return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

    def load(self) -> Identity | None:
        if not self.identity_path.exists():
            return None
        return Identity(**json.loads(self.identity_path.read_text(encoding="utf-8")))

    def save(self, identity: Identity) -> None:
        atomic_write(self.identity_path, json.dumps(asdict(identity), indent=2).encode(), 0o600)


def connection_token(key: Ed25519PrivateKey, server_id: str, now: float | None = None) -> str:
    issued = int(now if now is not None else time.time())
    return jwt.encode(
        {
            "iss": server_id,
            "sub": server_id,
            "aud": AUDIENCE,
            "iat": issued,
            "exp": issued + TOKEN_LIFETIME_S,
            "jti": secrets.token_hex(8),
        },
        key,
        algorithm="EdDSA",
        headers={"kid": server_id},
    )
