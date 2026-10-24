"""Agent releases (design §17.3): signed bundles, installed side by side, switched atomically and
rolled back automatically.

A release bundle is a tar holding ``release.json`` (name, version, and the SHA-256 of every
file), ``release.sig`` (an Ed25519 signature over the exact bytes of ``release.json``, by a Neura
release key), ``wheels/`` (the agent and its dependencies), ``bin/neura-agent-launch`` and the
installer, ``bin/install.sh``, which the one-line installer runs. The installer pins the release
keys on the server (``/etc/neura/release-keys.pem``); neither the control plane nor anyone else
can make a site run agent code that is not signed with them.
``openssl pkeyutl -verify -rawin`` checks the same signature, which is how the installer verifies
a bundle before anything is installed.

On the server (``/opt/neura/agent`` by default)::

    releases/<version>/venv     one virtual environment per release, never modified once staged
    current -> releases/<v>      the release systemd runs, switched by an atomic rename
    bin/neura-agent-launch       the stable launcher (neura_agent.launch), outside any release

An upgrade's progress is ``upgrade.json`` in the agent's state directory: staged, switching (the
new release is starting; the launcher counts its attempts), committed (it connected and serves),
or rolled back (it did not, and ``current`` points at the old release again).
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from neura_agent.identity import atomic_write

RELEASE_TYPE = "neura-agent-release"
LAUNCHER = "bin/neura-agent-launch"
INSTALLER = "bin/install.sh"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.+-]+)?$")
UPGRADE_FILE = "upgrade.json"


class ReleaseError(Exception):
    """A bundle is malformed, not signed by a trusted release key, or cannot be installed."""


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    files: dict[str, str]


def load_release_keys(path: Path) -> list[Ed25519PublicKey]:
    """PEM public keys (one or more) from the release-key file."""
    try:
        text = path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"no release keys at {path}: {exc.strerror}") from exc
    keys = []
    for block in re.findall(rb"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----", text, re.S):
        key = serialization.load_pem_public_key(block)
        if not isinstance(key, Ed25519PublicKey):
            raise ReleaseError("release keys must be Ed25519")
        keys.append(key)
    if not keys:
        raise ReleaseError(f"{path} holds no release key")
    return keys


def _signature(path: Path) -> bytes:
    raw = path.read_bytes()
    if len(raw) == 64:
        return raw
    try:
        decoded = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReleaseError("release.sig is not an Ed25519 signature") from exc
    if len(decoded) != 64:
        raise ReleaseError("release.sig is not an Ed25519 signature")
    return decoded


def verify_bundle(directory: Path, keys: list[Ed25519PublicKey]) -> Release:
    """Check the signature, then every file against the signed digests. Nothing else in the
    bundle is used."""
    try:
        data = (directory / "release.json").read_bytes()
        signature = _signature(directory / "release.sig")
    except OSError as exc:
        raise ReleaseError(f"not a release bundle: {exc}") from exc
    for key in keys:
        try:
            key.verify(signature, data)
            break
        except InvalidSignature:
            continue
    else:
        raise ReleaseError("the bundle is not signed by a release key this server trusts")
    manifest = json.loads(data)
    if manifest.get("type") != RELEASE_TYPE or manifest.get("name") != "neura-agent":
        raise ReleaseError("not a neura-agent release")
    version = str(manifest.get("version", ""))
    if not VERSION.match(version):
        raise ReleaseError(f"invalid release version {version!r}")
    files: dict[str, str] = manifest.get("files", {})
    if LAUNCHER not in files or not all(
        name.startswith("wheels/") or name in (LAUNCHER, INSTALLER) for name in files
    ):
        raise ReleaseError(f"a release holds wheels/, {LAUNCHER} and {INSTALLER}")
    for name, digest in files.items():
        path = (directory / name).resolve()
        if directory.resolve() not in path.parents:
            raise ReleaseError(f"unsafe path in the release: {name}")
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ReleaseError(f"{name} is missing from the bundle") from exc
        if actual != digest:
            raise ReleaseError(f"{name} does not match its signed digest")
    present = {f"wheels/{p.name}" for p in (directory / "wheels").iterdir()}
    if present - set(files):
        raise ReleaseError(f"files not in the signed release: {sorted(present - set(files))}")
    return Release(version, files)


def unpack(bundle: Path, destination: Path) -> Path:
    """Extract a bundle tar (only regular files and directories, inside ``destination``)."""
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    try:
        with tarfile.open(bundle) as tar:
            tar.extractall(destination, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError(f"cannot unpack {bundle.name}: {exc}") from exc
    entries = list(destination.iterdir())
    # Bundles may hold their files at the top or in one directory.
    if len(entries) == 1 and entries[0].is_dir() and not (destination / "release.json").exists():
        return entries[0]
    return destination


class Installation:
    """The releases installed on this server and the one that runs."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.releases = root / "releases"
        self.current_link = root / "current"

    @property
    def installed(self) -> bool:
        return self.current_link.is_symlink()

    def current_version(self) -> str | None:
        if not self.current_link.is_symlink():
            return None
        return Path(os.readlink(self.current_link)).name

    def has(self, version: str) -> bool:
        return (self.releases / version / ".complete").is_file()

    def stage(self, bundle_dir: Path, release: Release, python: str | None = None) -> Path:
        """A virtual environment for the release, installed from the bundle's wheels only."""
        target = self.releases / release.version
        if self.has(release.version):
            return target
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        interpreter = str(python or getattr(sys, "_base_executable", None) or sys.executable)
        try:
            subprocess.run(  # noqa: S603 - fixed arguments
                [interpreter, "-m", "venv", str(target / "venv")],
                check=True,
                capture_output=True,
                timeout=300,
            )
            subprocess.run(  # noqa: S603 - fixed arguments
                [
                    str(target / "venv" / "bin" / "python"),
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--find-links",
                    str(bundle_dir / "wheels"),
                    f"neura-agent=={release.version}",
                ],
                check=True,
                capture_output=True,
                timeout=1800,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            shutil.rmtree(target, ignore_errors=True)
            raise ReleaseError(
                f"installing {release.version} failed: {detail.decode(errors='replace')[-800:]}"
            ) from exc
        (target / ".complete").write_text(release.version)
        return target

    def switch(self, version: str) -> None:
        """Point ``current`` at a staged release, atomically."""
        if not self.has(version):
            raise ReleaseError(f"release {version} is not staged")
        temporary = self.root / f".current-{os.getpid()}"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / version)
        os.replace(temporary, self.current_link)

    def prune(self, keep: set[str]) -> None:
        if not self.releases.is_dir():
            return
        for path in self.releases.iterdir():
            if path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)


@dataclass(slots=True)
class UpgradeState:
    command_id: str
    operation_id: str
    from_version: str
    to_version: str
    state: str  # switching | committed | rolled_back
    attempts: int = 0
    switched_at: str = field(default_factory=lambda: _now())
    reason: str = ""
    rolled_back_at: str = ""

    @classmethod
    def load(cls, state_dir: Path) -> UpgradeState | None:
        path = state_dir / UPGRADE_FILE
        try:
            data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        # Another release may know more fields (the launcher, an older or newer agent).
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, state_dir: Path) -> None:
        atomic_write(state_dir / UPGRADE_FILE, json.dumps(asdict(self), indent=2).encode(), 0o600)

    @staticmethod
    def clear(state_dir: Path) -> None:
        (state_dir / UPGRADE_FILE).unlink(missing_ok=True)


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()
