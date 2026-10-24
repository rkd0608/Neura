"""The publisher key this machine signs policy versions with (design §17.3).

Made on first push at ``~/.config/neura/publisher.key`` (readable by its owner only) and
registered with the workspace, so the install commands Neura prints make new sites trust it. Sites
run only versions signed by a key they trust: neither Neura nor anyone holding a cloud credential
can make a site run something this key did not sign. Teams with a release process sign in CI
instead and keep the key there (``NEURA_PUBLISHER_KEY``: a path to a PEM file).
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from neura_cli.config import Config
from neura_cli.session import CliError, Session
from neura_contracts import signing


def path(config: Config) -> Path:
    configured = os.environ.get("NEURA_PUBLISHER_KEY")
    return Path(configured) if configured else config.publisher_key_path()


def load(config: Config, *, create: bool) -> Ed25519PrivateKey | None:
    key_path = path(config)
    if key_path.is_file():
        try:
            return signing.load_private_key(key_path.read_bytes())
        except signing.SignatureError as error:
            raise CliError(f"{key_path} is not a publisher key.", str(error)) from error
    if not create:
        return None
    if os.environ.get("NEURA_PUBLISHER_KEY"):
        raise CliError(f"NEURA_PUBLISHER_KEY names {key_path}, which does not exist.")
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pem = signing.generate_key()
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
    return signing.load_private_key(pem)


def public_hex(key: Ed25519PrivateKey) -> str:
    return signing.raw_public_key(key).hex()


def ensure_registered(session: Session, key: Ed25519PrivateKey) -> tuple[str, bool]:
    """Register the key with the workspace unless it is already. Returns (key id, newly)."""
    public = public_hex(key)
    known = session.client.paginate(session.client.list_publisher_keys, session.organization_id)
    for item in known:
        if item["spec"]["public_key"] == public:
            return item["status"]["key_id"], False
    created = session.client.create_publisher_key(
        session.organization_id,
        body={"public_key": public, "display_name": f"{_who()} (neura CLI)"[:200]},
    )
    return created["status"]["key_id"], True


def _who() -> str:
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "someone"
    return f"{user}@{socket.gethostname()}"
