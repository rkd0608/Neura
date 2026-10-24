"""Getting a version's files to its sites, without Neura ever reading them (design §18).

- **Sites on this machine** (joined with `neura site join`) read files from a directory laid
  out ``sha256/<hex>``: pushing puts them there.
- **Sealed upload** (``--seal-key FILE``): each file is encrypted with the customer's key and
  uploaded to Neura's registry, which holds only ciphertext. Sites holding the key open them.
- **The customer's own store** (an NFS share, an object store, a registry the sites are
  configured with): nothing to do here; the files are found there by digest.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from pathlib import Path

from neura_cli.session import CliError, Session
from neura_cli.sources import File
from neura_contracts import sealing


def to_directory(files: list[File], directory: Path) -> tuple[int, int]:
    """Place files under ``directory/sha256/<hex>``. Returns (added, already there)."""
    target = directory / "sha256"
    target.mkdir(parents=True, exist_ok=True)
    added = present = 0
    for item in files:
        destination = target / item.digest.removeprefix("sha256:")
        if destination.exists():
            present += 1
            continue
        descriptor, staged_name = tempfile.mkstemp(dir=target, prefix=".incoming-")
        os.close(descriptor)
        staged = Path(staged_name)
        try:
            # A copy, not a link: editing the checkpoint later must not change a pushed version.
            shutil.copyfile(item.source, staged)
            staged.chmod(0o444)
            staged.replace(destination)
        finally:
            with contextlib.suppress(FileNotFoundError):
                staged.unlink()
        added += 1
    return added, present


def sealed_upload(session: Session, files: list[File], key_file: Path) -> int:
    """Seal each file with the key in ``key_file`` and upload the ciphertext. Returns how many
    were uploaded."""
    try:
        key = sealing.FileKey(key_file)
    except (OSError, sealing.KeyUnavailable) as error:
        raise CliError(f"Could not read the sealing key {key_file}.", str(error)) from error
    uploaded = 0
    with tempfile.TemporaryDirectory(prefix="neura-sealed-") as scratch:
        for item in {f.digest: f for f in files}.values():
            sealed = Path(scratch) / item.digest.removeprefix("sha256:")
            with sealed.open("wb") as handle:
                header = sealing.seal_file(item.source, handle, key)
            if header.plaintext_digest != item.digest:
                raise CliError(f"{item.source} changed while it was being pushed.")
            session.client.upload_sealed_weights(session.project_id, sealed)
            sealed.unlink()
            uploaded += 1
    return uploaded
