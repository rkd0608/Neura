"""Sealed weights: weight files encrypted with a customer-managed key (design §18.1, §18.2).

When a customer stores weights anywhere they do not fully control (Neura's registry, a shared
bucket), each file is **sealed**: encrypted with a fresh random data key, which is in turn wrapped
by the customer's key-encryption key. Only the site agent unwraps it, through a key provider
configured on the server, so Neura never holds anything it can decrypt, and revoking the key in the
customer's KMS stops every further unseal.

Format (version 1)::

    MAGIC                      b"NEURASEAL1\\n"
    header length              4 bytes, big endian
    header                     canonical JSON: version, cipher, chunk_bytes, key_id,
                               wrapped_key (base64url), plaintext_digest, plaintext_bytes
    chunks                     AES-256-GCM, each chunk_bytes of plaintext (the last may be
                               shorter, and an empty file has one empty chunk) plus a 16-byte tag

Chunk ``i`` uses the nonce ``0x00000000 || i`` (64-bit big endian): each file has its own data
key, so a nonce never repeats under a key. Its associated data is ``sha256(header) || i || final``,
which binds every chunk to this header and position and makes truncation, reordering and header
edits fail authentication. The plaintext digest is checked again after decryption. It reveals
nothing new: the artifact document already lists every weight file's digest.

Key providers:

- :class:`FileKey`: a 256-bit key-encryption key in a file on the server, wrapping with AES key
  wrap (RFC 3394).
- :class:`CommandKey`: an external command that wraps and unwraps (``<command> wrap|unwrap``,
  key on stdin, result on stdout), the bridge to any KMS or HSM (AWS KMS, Google Cloud KMS, Azure
  Key Vault, HashiCorp Vault transit, PKCS #11) without Neura holding cloud credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from neura_contracts.canonical import canonical_json

MAGIC = b"NEURASEAL1\n"
VERSION = 1
CIPHER = "AES-256-GCM"
CHUNK_BYTES = 1 << 20
TAG_BYTES = 16
MAX_HEADER_BYTES = 64 * 1024
MAX_CHUNK_BYTES = 64 << 20


class SealError(ValueError):
    """A sealed file is malformed, was altered, or does not decrypt to what it declares."""


class KeyUnavailable(Exception):  # noqa: N818 - reads as a state, like FileNotFoundError
    """The key a sealed file needs is not configured here, or its provider refused or failed
    (for example, access was revoked in the customer's KMS)."""


class KeyProvider(Protocol):
    @property
    def key_id(self) -> str: ...

    def wrap(self, data_key: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- key providers ------------------------------------------------------------------------------


def generate_key_file(path: Path) -> str:
    """Write a new 256-bit key-encryption key (hex, mode 0600). Returns its key_id."""
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.hex() + "\n")
    return FileKey(path).key_id


class FileKey:
    """A key-encryption key held in a file: 32 raw bytes, or 64 hex characters."""

    def __init__(self, path: Path) -> None:
        self.path = path
        data = path.read_bytes()
        text = data.strip()
        if len(data) == 32:
            self._key = data
        elif len(text) == 64:
            try:
                self._key = bytes.fromhex(text.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise KeyUnavailable(f"{path} is not a 256-bit key") from exc
        else:
            raise KeyUnavailable(f"{path} is not a 256-bit key (32 bytes or 64 hex characters)")
        # Names the key without revealing anything about it.
        digest = hashlib.sha256(b"neura-key-id-v1\x00" + self._key).hexdigest()[:16]
        self._key_id = f"file:{digest}"

    @property
    def key_id(self) -> str:
        return self._key_id

    def wrap(self, data_key: bytes) -> bytes:
        return aes_key_wrap(self._key, data_key)

    def unwrap(self, wrapped: bytes) -> bytes:
        try:
            return aes_key_unwrap(self._key, wrapped)
        except InvalidUnwrap as exc:
            raise SealError("the wrapped data key does not unwrap with this key") from exc


class CommandKey:
    """A key held by a KMS or HSM, reached through a command::

        <command...> wrap      data key on stdin  -> wrapped key on stdout
        <command...> unwrap    wrapped key on stdin -> data key on stdout

    The command gets ``NEURA_KEY_ID`` in its environment. Exit status other than 0, a timeout or
    a malformed result means the key is unavailable."""

    def __init__(self, key_id: str, command: Sequence[str], timeout_s: float = 30.0) -> None:
        if not key_id or not command:
            raise ValueError("a command key needs a key_id and a command")
        self._key_id = key_id
        self.command = list(command)
        self.timeout_s = timeout_s

    @property
    def key_id(self) -> str:
        return self._key_id

    def _run(self, operation: str, data: bytes) -> bytes:
        try:
            done = subprocess.run(  # noqa: S603 - the operator configured this command
                [*self.command, operation],
                input=data,
                capture_output=True,
                timeout=self.timeout_s,
                env={**os.environ, "NEURA_KEY_ID": self._key_id},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KeyUnavailable(f"key {self._key_id}: {operation} failed: {exc}") from exc
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip()[-300:]
            raise KeyUnavailable(
                f"key {self._key_id}: {operation} exited {done.returncode}: {detail}"
            )
        return done.stdout

    def wrap(self, data_key: bytes) -> bytes:
        wrapped = self._run("wrap", data_key)
        if not wrapped:
            raise KeyUnavailable(f"key {self._key_id}: wrap returned nothing")
        return wrapped

    def unwrap(self, wrapped: bytes) -> bytes:
        data_key = self._run("unwrap", wrapped)
        if len(data_key) != 32:
            raise KeyUnavailable(
                f"key {self._key_id}: unwrap returned {len(data_key)} bytes, expected 32"
            )
        return data_key


# --- sealing ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Header:
    key_id: str
    wrapped_key: bytes
    plaintext_digest: str
    plaintext_bytes: int
    chunk_bytes: int
    encoded: bytes

    @property
    def chunks(self) -> int:
        return max(1, -(-self.plaintext_bytes // self.chunk_bytes))


def _nonce(index: int) -> bytes:
    return b"\x00\x00\x00\x00" + index.to_bytes(8, "big")


def _aad(header_digest: bytes, index: int, final: bool) -> bytes:
    return header_digest + index.to_bytes(8, "big") + (b"\x01" if final else b"\x00")


def _digest_file(path: Path) -> tuple[str, int]:
    hasher, size = hashlib.sha256(), 0
    with path.open("rb") as f:
        while block := f.read(CHUNK_BYTES):
            hasher.update(block)
            size += len(block)
    return f"sha256:{hasher.hexdigest()}", size


def seal_file(
    source: Path, destination: BinaryIO, key: KeyProvider, *, chunk_bytes: int = CHUNK_BYTES
) -> Header:
    """Seal ``source`` into ``destination``. Reads the file twice (digest, then encrypt)."""
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES:
        raise ValueError(f"chunk_bytes must be between 1 and {MAX_CHUNK_BYTES}")
    digest, size = _digest_file(source)
    data_key = AESGCM.generate_key(bit_length=256)
    wrapped = key.wrap(data_key)
    encoded = canonical_json(
        {
            "version": VERSION,
            "cipher": CIPHER,
            "chunk_bytes": chunk_bytes,
            "key_id": key.key_id,
            "wrapped_key": _b64(wrapped),
            "plaintext_digest": digest,
            "plaintext_bytes": size,
        }
    )
    header = Header(key.key_id, wrapped, digest, size, chunk_bytes, encoded)
    header_digest = hashlib.sha256(encoded).digest()
    destination.write(MAGIC + len(encoded).to_bytes(4, "big") + encoded)
    aead = AESGCM(data_key)
    with source.open("rb") as f:
        for index in range(header.chunks):
            block = f.read(chunk_bytes)
            final = index == header.chunks - 1
            aad = _aad(header_digest, index, final)
            destination.write(aead.encrypt(_nonce(index), block, aad))
        if f.read(1):
            raise SealError(f"{source} changed while it was being sealed")
    return header


def is_sealed(prefix: bytes) -> bool:
    return prefix.startswith(MAGIC)


def read_header(stream: BinaryIO) -> Header:
    magic = stream.read(len(MAGIC))
    if magic != MAGIC:
        raise SealError("not a sealed file")
    length = int.from_bytes(stream.read(4), "big")
    if not 2 <= length <= MAX_HEADER_BYTES:
        raise SealError("sealed header length out of range")
    encoded = stream.read(length)
    if len(encoded) != length:
        raise SealError("sealed file truncated in its header")
    try:
        fields = json.loads(encoded)
        if canonical_json(fields) != encoded:
            raise SealError("sealed header is not canonical JSON")
        if fields["version"] != VERSION or fields["cipher"] != CIPHER:
            raise SealError(f"unsupported sealed format {fields['version']}/{fields['cipher']}")
        chunk_bytes = int(fields["chunk_bytes"])
        size = int(fields["plaintext_bytes"])
        digest = str(fields["plaintext_digest"])
        header = Header(
            key_id=str(fields["key_id"]),
            wrapped_key=_unb64(str(fields["wrapped_key"])),
            plaintext_digest=digest,
            plaintext_bytes=size,
            chunk_bytes=chunk_bytes,
            encoded=encoded,
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SealError):
            raise
        raise SealError(f"malformed sealed header: {exc}") from exc
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES or size < 0:
        raise SealError("sealed header sizes out of range")
    if not (digest.startswith("sha256:") and len(digest) == 71):
        raise SealError("sealed header digest is not sha256")
    return header


def unseal(
    stream: BinaryIO, destination: BinaryIO, keys: Mapping[str, KeyProvider]
) -> tuple[str, int]:
    """Decrypt a sealed stream into ``destination``. Returns (plaintext digest, size), both
    checked against the header. Raises KeyUnavailable or SealError; on SealError the
    destination holds partial plaintext that must be discarded."""
    header = read_header(stream)
    provider = keys.get(header.key_id)
    if provider is None:
        configured = ", ".join(sorted(keys)) or "none"
        raise KeyUnavailable(f"sealed with key {header.key_id}; this server has keys: {configured}")
    data_key = provider.unwrap(header.wrapped_key)
    if len(data_key) != 32:
        raise SealError("the data key is not 256 bits")
    aead = AESGCM(data_key)
    header_digest = hashlib.sha256(header.encoded).digest()
    hasher, size = hashlib.sha256(), 0
    for index in range(header.chunks):
        final = index == header.chunks - 1
        expected = (
            header.plaintext_bytes - index * header.chunk_bytes if final else header.chunk_bytes
        )
        ciphertext = stream.read(expected + TAG_BYTES)
        if len(ciphertext) != expected + TAG_BYTES:
            raise SealError("sealed file truncated")
        try:
            block = aead.decrypt(_nonce(index), ciphertext, _aad(header_digest, index, final))
        except InvalidTag as exc:
            raise SealError(f"chunk {index} failed authentication: altered or corrupted") from exc
        hasher.update(block)
        size += len(block)
        destination.write(block)
    if stream.read(1):
        raise SealError("trailing data after the last sealed chunk")
    digest = f"sha256:{hasher.hexdigest()}"
    if digest != header.plaintext_digest or size != header.plaintext_bytes:
        raise SealError("decrypted content does not match the sealed header")
    return digest, size


def seal_directory(directory: Path, out: Path, key: KeyProvider) -> list[tuple[str, str, int]]:
    """Seal every regular file under ``directory`` into ``out/sha256/<hex>``, the layout artifact
    sources use. Returns (relative path, plaintext digest, size) per file; identical files are
    sealed once."""
    files = sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
    target = out / "sha256"
    target.mkdir(parents=True, exist_ok=True)
    sealed: list[tuple[str, str, int]] = []
    for path in files:
        digest, size = _digest_file(path)
        name = target / digest.removeprefix("sha256:")
        if not name.exists():
            staging = name.with_suffix(".partial")
            with staging.open("wb") as f:
                header = seal_file(path, f, key)
            if header.plaintext_digest != digest:
                staging.unlink()
                raise SealError(f"{path} changed while it was being sealed")
            staging.replace(name)
        sealed.append((path.relative_to(directory).as_posix(), digest, size))
    return sealed
