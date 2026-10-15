"""Publisher signatures on model artifacts (design §17.3, §17.4, §18.2).

An artifact's digest covers its whole canonical document: every weight file's digest and size, the
runtime image pinned by digest, the adapter and its versions. A **publisher** (the customer's
release process) signs that digest with an Ed25519 key. Each site runs only artifacts signed by a
key its operator trusts, configured on the server itself, so neither Neura nor a compromised
control plane can make a site load weights or code that the customer did not publish.

Signatures travel next to the artifact: the API stores them and the site manifest carries them.
Adding one never changes the artifact's digest. A signature is a small JSON object::

    {"key_id": "ed25519:<16 hex>", "public_key": "<64 hex>", "signature": "<base64url>"}

over ``SIGNATURE_CONTEXT || artifact_digest``. Private keys are PEM (PKCS #8, the format
``openssl genpkey -algorithm ed25519`` writes); public keys are 64 hex characters or PEM.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from neura_contracts.reason_codes import ReasonCode

SIGNATURE_CONTEXT = b"neura-artifact-signature-v1\x00"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_SIGNATURES = 16


class SignatureError(ValueError):
    """A key or signature is malformed, or an artifact is not signed as this site requires."""

    def __init__(self, message: str, code: ReasonCode = ReasonCode.API_VALIDATION_FAILED) -> None:
        super().__init__(message)
        self.code = code


def key_id(public_key: bytes) -> str:
    return "ed25519:" + hashlib.sha256(public_key).hexdigest()[:16]


def generate_key() -> bytes:
    """A new publisher private key, PEM."""
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise SignatureError(f"not a PEM private key: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("publisher keys must be Ed25519")
    return key


def raw_public_key(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def parse_public_key(text: str | bytes) -> bytes:
    """A publisher public key from 64 hex characters or a PEM public key. Returns the raw 32
    bytes."""
    data = text.encode() if isinstance(text, str) else text
    stripped = data.strip()
    if stripped.startswith(b"-----BEGIN"):
        try:
            key = serialization.load_pem_public_key(stripped)
        except ValueError as exc:
            raise SignatureError(f"not a PEM public key: {exc}") from exc
        if not isinstance(key, Ed25519PublicKey):
            raise SignatureError("publisher keys must be Ed25519")
        return raw_public_key(key)
    try:
        raw = bytes.fromhex(stripped.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SignatureError("a public key is 64 hex characters or PEM") from exc
    if len(raw) != 32:
        raise SignatureError("a public key is 64 hex characters or PEM")
    return raw


def parse_public_keys(text: str) -> list[bytes]:
    """Keys from a trust file: PEM blocks and hex lines; blank lines and ``#`` comments are
    ignored."""
    keys: list[bytes] = []
    for block in re.findall(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", text, re.S):
        keys.append(parse_public_key(block))
    rest = re.sub(r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", "", text, flags=re.S)
    for line in rest.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            keys.append(parse_public_key(line))
    return keys


def _message(artifact_digest: str) -> bytes:
    if not DIGEST.match(artifact_digest):
        raise SignatureError(f"not an artifact digest: {artifact_digest!r}")
    return SIGNATURE_CONTEXT + artifact_digest.encode("ascii")


def sign(artifact_digest: str, private_key: Ed25519PrivateKey) -> dict[str, str]:
    public = raw_public_key(private_key)
    signature = private_key.sign(_message(artifact_digest))
    return {
        "key_id": key_id(public),
        "public_key": public.hex(),
        "signature": base64.urlsafe_b64encode(signature).decode().rstrip("="),
    }


def _decode(signature: Mapping[str, Any]) -> tuple[bytes, bytes]:
    if not isinstance(signature, Mapping) or set(signature) != {
        "key_id",
        "public_key",
        "signature",
    }:
        raise SignatureError("a signature has exactly key_id, public_key and signature")
    public = parse_public_key(str(signature["public_key"]))
    if signature["key_id"] != key_id(public):
        raise SignatureError("a signature's key_id does not match its public key")
    text = str(signature["signature"])
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise SignatureError("a signature is base64url") from exc
    if len(raw) != 64:
        raise SignatureError("an Ed25519 signature is 64 bytes")
    return public, raw


def verify(artifact_digest: str, signature: Mapping[str, Any]) -> bool:
    """Whether the signature is valid for the digest. Raises SignatureError if malformed."""
    public, raw = _decode(signature)
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(raw, _message(artifact_digest))
    except InvalidSignature:
        return False
    return True


def validate(artifact_digest: str, signatures: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Signatures as the API accepts them: well formed, valid, at most one per key, sorted by
    key_id. Raises SignatureError naming the first problem."""
    by_key: dict[str, dict[str, str]] = {}
    for signature in signatures:
        if not verify(artifact_digest, signature):
            raise SignatureError(
                f"the signature by {signature['key_id']} is not valid for {artifact_digest}"
            )
        by_key[str(signature["key_id"])] = {k: str(v) for k, v in signature.items()}
    if len(by_key) > MAX_SIGNATURES:
        raise SignatureError(f"an artifact holds at most {MAX_SIGNATURES} signatures")
    return [by_key[k] for k in sorted(by_key)]


def trusted_signer(
    artifact_digest: str,
    signatures: Iterable[Mapping[str, Any]],
    trusted: Iterable[bytes],
) -> str:
    """The key_id of a trusted publisher whose signature on the digest verifies.

    Raises SignatureError with ``AGENT_ARTIFACT_SIGNATURE_INVALID`` if a trusted key's signature
    does not verify (tampering, or a signature for another artifact), else with
    ``AGENT_ARTIFACT_UNTRUSTED`` if no signature is by a trusted key."""
    allowed = set(trusted)
    seen: list[str] = []
    for signature in signatures:
        try:
            public, _ = _decode(signature)
        except SignatureError:
            continue  # a malformed signature from anyone is just not a trusted one
        if public not in allowed:
            seen.append(str(signature.get("key_id")))
            continue
        if not verify(artifact_digest, signature):
            raise SignatureError(
                f"the signature by trusted key {key_id(public)} does not verify for "
                f"{artifact_digest}",
                ReasonCode.AGENT_ARTIFACT_SIGNATURE_INVALID,
            )
        return key_id(public)
    trusted_ids = ", ".join(sorted(key_id(k) for k in allowed)) or "none configured"
    signed_by = ", ".join(seen) or "nobody"
    raise SignatureError(
        f"{artifact_digest} is signed by {signed_by}; this server trusts {trusted_ids}",
        ReasonCode.AGENT_ARTIFACT_UNTRUSTED,
    )
