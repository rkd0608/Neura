"""Neura's registry of sealed weights (design §18.1, §18.2).

Customers who choose to keep weights with Neura upload each file sealed with their own key
(``neura-contracts seal``). The control plane checks that an upload is a well-formed sealed file
for the digest it is stored under, but holds no key and cannot open it. Site agents fetch sealed
files over their authenticated channel and open them with the customer's key at the site.

Files live under ``<root>/<project_id>/sha256/<hex>``; uploads are staged and renamed into place,
so a reader never sees a partial file. A file changes only after its record is committed
(``place``): a change that fails before its commit leaves the file as its record says.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from neura_contracts import sealing
from neura_control.db.models import SealedWeights

CHUNK = 1 << 20
# A staged upload is written to until it is moved into place. One untouched this long was left by a
# process that stopped while receiving it; a newer one may be another replica's upload in progress.
ABANDONED_S = 3600


class SealedUploadError(ValueError):
    """The upload is not a complete sealed file for the digest it is stored under."""


@dataclass(frozen=True, slots=True)
class StoredWeights:
    key_id: str
    plaintext_bytes: int
    sealed_bytes: int
    sealed_sha256: str


@dataclass(frozen=True, slots=True)
class Staged:
    path: Path
    target: Path
    info: StoredWeights


def _hex(digest: str) -> str:
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise SealedUploadError(f"not a sha256 digest: {digest}")
    return value


def expected_size(header: sealing.Header) -> int:
    return (
        len(sealing.MAGIC)
        + 4
        + len(header.encoded)
        + header.plaintext_bytes
        + header.chunks * sealing.TAG_BYTES
    )


class SealedWeightsStore:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._clear_abandoned()

    def _clear_abandoned(self) -> None:
        incoming = self.root / ".incoming"
        if not incoming.is_dir():
            return
        cutoff = time.time() - ABANDONED_S
        for path in incoming.iterdir():
            try:
                abandoned = path.stat().st_mtime < cutoff
            except FileNotFoundError:
                continue  # moved into place or discarded meanwhile
            if abandoned:
                path.unlink(missing_ok=True)

    def path(self, project_id: str, digest: str) -> Path:
        return self.root / project_id / "sha256" / _hex(digest)

    async def stage(self, project_id: str, digest: str, body: AsyncIterator[bytes]) -> Staged:
        """Stream an upload to a staging file after checking its header and length. Raises
        SealedUploadError. The caller commits or discards the result."""
        incoming = self.root / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=incoming)
        staged = Path(name)
        try:
            hasher, size, head = hashlib.sha256(), 0, b""
            header: sealing.Header | None = None
            with os.fdopen(fd, "wb") as out:
                async for block in body:
                    size += len(block)
                    if size > self.max_bytes:
                        raise SealedUploadError(f"larger than {self.max_bytes} bytes")
                    hasher.update(block)
                    out.write(block)
                    if header is None:
                        head += block
                        header = _header(head, digest)
                        if header is not None:
                            head = b""
                out.flush()
                os.fsync(out.fileno())
            if header is None:
                header = _header(head, digest, final=True)
            assert header is not None  # _header raises when final
            if size != expected_size(header):
                raise SealedUploadError(
                    f"{size} bytes, a sealed file with this header has {expected_size(header)}"
                )
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
        info = StoredWeights(header.key_id, header.plaintext_bytes, size, hasher.hexdigest())
        return Staged(staged, self.path(project_id, digest), info)

    @staticmethod
    def commit(staged: Staged) -> None:
        """Move a staged upload into place (atomically replacing an earlier one)."""
        staged.target.parent.mkdir(parents=True, exist_ok=True)
        staged.path.chmod(0o444)
        os.replace(staged.path, staged.target)

    @staticmethod
    def discard(staged: Staged) -> None:
        staged.path.unlink(missing_ok=True)

    def read(self, project_id: str, digest: str) -> Path | None:
        path = self.path(project_id, digest)
        return path if path.is_file() else None

    def delete(self, project_id: str, digest: str) -> None:
        self.path(project_id, digest).unlink(missing_ok=True)


async def place(
    session: AsyncSession,
    store: SealedWeightsStore,
    project_id: str,
    digest: str,
    staged: Staged | None = None,
) -> None:
    """Make the stored file follow the committed record, in a transaction after the one that
    changed it: an upload's staged file goes into place if the record is still that upload's, and
    a file whose record is gone is removed. The file steps of one digest are serialized by an
    advisory lock, which holds even when no row is left to lock."""
    key = func.hashtextextended(f"sealed-weights:{project_id}/{digest}", 0)
    await session.execute(select(func.pg_advisory_xact_lock(key)))
    row = await session.get(SealedWeights, (project_id, digest), populate_existing=True)
    if row is None:
        store.delete(project_id, digest)
    elif staged is not None and row.sealed_sha256 == staged.info.sealed_sha256:
        store.commit(staged)


def _header(head: bytes, digest: str, *, final: bool = False) -> sealing.Header | None:
    """The sealed header once enough of the upload has arrived (None until then)."""
    prefix = len(sealing.MAGIC) + 4
    if len(head) >= len(sealing.MAGIC) and not sealing.is_sealed(head):
        raise SealedUploadError("not a sealed file: seal weights with `neura-contracts seal`")
    if len(head) >= prefix:
        length = int.from_bytes(head[len(sealing.MAGIC) : prefix], "big")
        if not 2 <= length <= sealing.MAX_HEADER_BYTES:  # before buffering that much
            raise SealedUploadError("sealed header length out of range")
        if len(head) >= prefix + length:
            try:
                header = sealing.read_header(io.BytesIO(head))
            except sealing.SealError as exc:
                raise SealedUploadError(str(exc)) from exc
            if header.plaintext_digest != digest:
                raise SealedUploadError(
                    f"sealed for {header.plaintext_digest}, uploaded as {digest}"
                )
            return header
    if final:
        raise SealedUploadError("truncated before the end of its sealed header")
    return None
