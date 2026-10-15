"""The site's artifact store: model weight files, content-addressed and verified (design §13, §18).

Weights stay with the customer (§18.1): the control plane only ever sees their digests. Files reach
the store in one of two ways:

- ``neura-agent import-weights DIR``: an operator copies a checkpoint onto the server and imports
  it. Every file is hashed and stored under its SHA-256, so the import needs no manifest.
- configured sources (``NEURA_AGENT_ARTIFACT_SOURCES``): directories (for example a mounted NFS
  share) or HTTPS base URLs (for example the customer's own registry or object store) laid out
  as ``<source>/sha256/<hex>``, or ``neura`` for Neura's registry, which holds sealed files only.
  The agent fetches what a prepared revision needs.

A source may hold **sealed** files (``neura-contracts seal``: encrypted with a customer-managed
key, §18.2) under the same names. The agent recognizes them, opens them with the keys configured
on this server (``NEURA_AGENT_ARTIFACT_KEYS``) and verifies the plaintext like any other file.

Every file is verified against its digest before it is stored; nothing unverified is ever read by
a worker. A prepared artifact is materialized as a read-only checkout directory whose files are
hard links to the verified blobs, laid out as the artifact's manifest names them.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import httpx

from neura_agent.settings import ArtifactKey
from neura_contracts import ModelArtifact, ReasonCode, sealing

CHUNK = 1 << 20
REGISTRY_SOURCE = "neura"  # Neura's registry of sealed weights (neura_agent.registry)


class ArtifactError(Exception):
    def __init__(self, code: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImportedFile:
    path: str
    digest: str
    size_bytes: int


def _hex(digest: str) -> str:
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64:
        raise ArtifactError(ReasonCode.CONTRACT_INVALID, f"unsupported digest {digest}")
    return value


class ArtifactStore:
    def __init__(
        self,
        root: Path,
        sources: list[str] | None = None,
        token: str | None = None,
        keys: Mapping[str, sealing.KeyProvider] | None = None,
        registry: Callable[[str, BinaryIO], bool] | None = None,
    ):
        self.root = root
        self.sources = list(sources or [])
        self._token = token
        self.keys = dict(keys or {})
        # The ``neura`` source: Neura's registry of sealed weights, reached through the agent's
        # authenticated channel (set once the server is enrolled).
        self.registry = registry
        self._blobs = root / "blobs" / "sha256"
        self._checkouts = root / "checkouts"
        self._incoming = root / "incoming"

    def blob(self, digest: str) -> Path:
        return self._blobs / _hex(digest)

    def has(self, digest: str) -> bool:
        return self.blob(digest).is_file()

    def checkout_path(self, artifact: ModelArtifact) -> Path:
        return self._checkouts / _hex(artifact.digest())

    # --- adding files -------------------------------------------------------------------------

    @contextlib.contextmanager
    def _staging(self) -> Iterator[tuple[Path, hashlib._Hash]]:
        self._incoming.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self._incoming)
        os.close(fd)
        path = Path(name)
        try:
            yield path, hashlib.sha256()
        finally:
            path.unlink(missing_ok=True)

    def _commit(self, staged: Path, digest: str) -> Path:
        target = self.blob(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged.chmod(0o444)
        os.replace(staged, target)
        return target

    def add_file(self, source: Path, expected: str | None = None) -> ImportedFile:
        """Copy a file into the store, hashing it on the way, or open it if it is sealed. Raises
        on a digest mismatch."""
        with source.open("rb") as src:
            if sealing.is_sealed(src.read(len(sealing.MAGIC))):
                src.seek(0)
                return self._unseal(src, str(source), expected)
            src.seek(0)
            with self._staging() as (staged, hasher):
                with staged.open("wb") as dst:
                    while block := src.read(CHUNK):
                        hasher.update(block)
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                digest = f"sha256:{hasher.hexdigest()}"
                self._check(digest, expected, str(source))
                size = staged.stat().st_size
                if not self.has(digest):
                    self._commit(staged, digest)
        return ImportedFile(str(source), digest, size)

    @staticmethod
    def _check(digest: str, expected: str | None, origin: str) -> None:
        if expected is not None and digest != expected:
            raise ArtifactError(
                ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
                f"{origin} has digest {digest}, expected {expected}",
            )

    def _unseal(self, stream: BinaryIO, origin: str, expected: str | None) -> ImportedFile:
        with self._staging() as (staged, _):
            try:
                with staged.open("wb") as dst:
                    digest, size = sealing.unseal(stream, dst, self.keys)
                    dst.flush()
                    os.fsync(dst.fileno())
            except sealing.KeyUnavailable as exc:
                raise ArtifactError(
                    ReasonCode.AGENT_ARTIFACT_KEY_UNAVAILABLE, f"{origin}: {exc}"
                ) from exc
            except sealing.SealError as exc:
                raise ArtifactError(
                    ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH, f"{origin}: {exc}"
                ) from exc
            self._check(digest, expected, origin)
            if not self.has(digest):
                self._commit(staged, digest)
        return ImportedFile(origin, digest, size)

    def import_directory(self, directory: Path) -> list[ImportedFile]:
        """Import every regular file under a directory (e.g. a LeRobot checkpoint)."""
        files = sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink())
        imported = []
        for path in files:
            item = self.add_file(path)
            imported.append(ImportedFile(path.relative_to(directory).as_posix(), *_tail(item)))
        return imported

    def _fetch(self, digest: str) -> bool:
        """Try each configured source. Returns True once the blob is stored."""
        value = _hex(digest)
        for source in self.sources:
            if source == REGISTRY_SOURCE:
                if self.registry is not None and self._from_registry(self.registry, digest):
                    return True
            elif source.startswith(("https://", "http://")):
                if self._download(f"{source.rstrip('/')}/sha256/{value}", digest):
                    return True
            else:
                candidate = Path(source) / "sha256" / value
                if candidate.is_file():
                    self.add_file(candidate, expected=digest)
                    return True
        return False

    def _from_registry(self, registry: Callable[[str, BinaryIO], bool], digest: str) -> bool:
        with self._staging() as (staged, _):
            with staged.open("wb") as out:
                if not registry(digest, out):
                    return False
            with staged.open("rb") as fetched:
                if not sealing.is_sealed(fetched.read(len(sealing.MAGIC))):
                    # Neura holds sealed weights only (design §18.2); anything else is refused.
                    raise ArtifactError(
                        ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
                        f"Neura's registry returned an unsealed file for {digest}",
                    )
                fetched.seek(0)
                self._unseal(fetched, f"{REGISTRY_SOURCE}:{digest}", digest)
        return True

    def _download(self, url: str, digest: str) -> bool:
        # The source token travels only over TLS (httpx drops it on a redirect to another host).
        secure = url.startswith("https://")
        headers = {"Authorization": f"Bearer {self._token}"} if self._token and secure else {}
        with self._staging() as (staged, hasher):
            try:
                with (
                    httpx.stream(
                        "GET", url, headers=headers, timeout=60, follow_redirects=True
                    ) as r,
                    staged.open("wb") as dst,
                ):
                    if r.status_code == 404:
                        return False
                    r.raise_for_status()
                    for block in r.iter_bytes(CHUNK):
                        hasher.update(block)
                        dst.write(block)
            except httpx.HTTPError:
                return False
            with staged.open("rb") as downloaded:
                if sealing.is_sealed(downloaded.read(len(sealing.MAGIC))):
                    downloaded.seek(0)
                    self._unseal(downloaded, url, digest)
                    return True
            if f"sha256:{hasher.hexdigest()}" != digest:
                raise ArtifactError(
                    ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
                    f"{url} does not match its digest {digest}",
                )
            self._commit(staged, digest)
        return True

    # --- checkouts ----------------------------------------------------------------------------

    def missing(self, artifact: ModelArtifact) -> list[str]:
        return [w.path for w in artifact.spec.weights if not self.has(w.digest)]

    def checkout(self, artifact: ModelArtifact) -> Path:
        """The artifact's weights as a read-only directory, fetching missing files from the
        configured sources first."""
        for weight in artifact.spec.weights:
            if not self.has(weight.digest):
                self._fetch(weight.digest)
        missing = self.missing(artifact)
        if missing:
            where = ", ".join(self.sources) if self.sources else "no sources configured"
            raise ArtifactError(
                ReasonCode.AGENT_ARTIFACT_UNAVAILABLE,
                f"weights not at this site: {', '.join(missing)} ({where}); copy the checkpoint "
                "to the server and run `neura-agent import-weights DIR`",
            )
        for weight in artifact.spec.weights:
            size = self.blob(weight.digest).stat().st_size
            if size != weight.size_bytes:
                raise ArtifactError(
                    ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH,
                    f"{weight.path} has {size} bytes, the artifact declares {weight.size_bytes}",
                )
        target = self.checkout_path(artifact)
        if target.is_dir() and all((target / w.path).is_file() for w in artifact.spec.weights):
            return target
        self._checkouts.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self._checkouts, prefix=".staging-"))
        try:
            for weight in artifact.spec.weights:
                path = staging / weight.path
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(self.blob(weight.digest), path)
                except OSError:  # another filesystem: copy (still from the verified blob)
                    shutil.copyfile(self.blob(weight.digest), path)
                    path.chmod(0o444)
            if target.exists():
                shutil.rmtree(target)
            os.replace(staging, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return target

    def prune_checkouts(self, keep: list[ModelArtifact]) -> None:
        """Remove checkouts no kept artifact needs. Blobs stay: an operator may have imported
        weights for a revision that is not deployed yet."""
        names = {self.checkout_path(a).name for a in keep}
        if self._checkouts.is_dir():
            for path in self._checkouts.iterdir():
                if path.name not in names and not path.name.startswith(".staging-"):
                    shutil.rmtree(path, ignore_errors=True)


def load_keys(configs: Sequence[ArtifactKey]) -> dict[str, sealing.KeyProvider]:
    """The configured key providers by key id. A key file that cannot be read fails startup."""
    keys: dict[str, sealing.KeyProvider] = {}
    for config in configs:
        provider: sealing.KeyProvider
        if config.file is not None:
            provider = sealing.FileKey(config.file)
            if config.key_id and config.key_id != provider.key_id:
                raise ValueError(f"{config.file} is key {provider.key_id}, not {config.key_id}")
        else:
            assert config.command is not None  # ArtifactKey requires file or command
            assert config.key_id  # and a key_id with a command
            provider = sealing.CommandKey(config.key_id, config.command)
        keys[provider.key_id] = provider
    return keys


def _tail(item: ImportedFile) -> tuple[str, int]:
    return item.digest, item.size_bytes
