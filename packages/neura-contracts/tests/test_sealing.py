"""Sealed weights: encrypted with a customer-managed key, opened only with it, and any change to
the sealed bytes is detected."""

from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path

import pytest

from neura_contracts import cli, sealing


@pytest.fixture
def key(tmp_path: Path) -> sealing.FileKey:
    path = tmp_path / "kek"
    sealing.generate_key_file(path)
    assert path.stat().st_mode & 0o777 == 0o600
    return sealing.FileKey(path)


def sealed(tmp_path: Path, data: bytes, key: sealing.KeyProvider, chunk: int = 1000) -> bytes:
    source = tmp_path / "weights.bin"
    source.write_bytes(data)
    out = io.BytesIO()
    header = sealing.seal_file(source, out, key, chunk_bytes=chunk)
    assert header.plaintext_digest == "sha256:" + hashlib.sha256(data).hexdigest()
    return out.getvalue()


def opened(blob: bytes, *keys: sealing.KeyProvider) -> bytes:
    out = io.BytesIO()
    sealing.unseal(io.BytesIO(blob), out, {k.key_id: k for k in keys})
    return out.getvalue()


@pytest.mark.parametrize("size", [0, 1, 999, 1000, 1001, 3500])
def test_sealed_files_open_to_their_plaintext(
    tmp_path: Path, key: sealing.FileKey, size: int
) -> None:
    data = os.urandom(size)
    blob = sealed(tmp_path, data, key)
    assert sealing.is_sealed(blob)
    if size > 100:  # long enough that finding it by chance in the ciphertext is impossible
        assert data not in blob
    assert opened(blob, key) == data


def test_a_different_key_cannot_open_it(tmp_path: Path, key: sealing.FileKey) -> None:
    blob = sealed(tmp_path, b"weights" * 100, key)
    other_path = tmp_path / "other"
    sealing.generate_key_file(other_path)
    other = sealing.FileKey(other_path)
    with pytest.raises(sealing.KeyUnavailable, match=key.key_id):
        opened(blob, other)
    # Same key id, different key (a forged header): unwrap fails.
    impostor = type("Impostor", (), {"key_id": key.key_id, "unwrap": other.unwrap})()
    with pytest.raises(sealing.SealError):
        opened(blob, impostor)


@pytest.mark.parametrize("where", ["header", "first_chunk", "last_chunk", "tag"])
def test_any_change_to_the_sealed_bytes_is_detected(
    tmp_path: Path, key: sealing.FileKey, where: str
) -> None:
    blob = bytearray(sealed(tmp_path, os.urandom(2500), key))
    header_end = len(sealing.MAGIC) + 4 + int.from_bytes(blob[len(sealing.MAGIC) :][:4], "big")
    offset = {
        "header": header_end - 5,
        "first_chunk": header_end + 10,
        "last_chunk": len(blob) - 30,
        "tag": len(blob) - 1,
    }[where]
    blob[offset] ^= 0x01
    with pytest.raises(sealing.SealError):
        opened(bytes(blob), key)


def test_truncation_reordering_and_trailing_data_are_detected(
    tmp_path: Path, key: sealing.FileKey
) -> None:
    blob = sealed(tmp_path, os.urandom(3000), key)
    header_end = len(blob) - 3 * (1000 + sealing.TAG_BYTES)
    chunks = [blob[header_end + i * 1016 : header_end + (i + 1) * 1016] for i in range(3)]
    for bad in (
        blob[:-1],
        blob[: header_end + 2 * 1016],  # the last chunk dropped
        blob[:header_end] + chunks[1] + chunks[0] + chunks[2],
        blob + b"x",
    ):
        with pytest.raises(sealing.SealError):
            opened(bad, key)


KMS = """
import os, sys
from cryptography.hazmat.primitives.keywrap import aes_key_unwrap, aes_key_wrap
kek = bytes.fromhex(open(os.environ["TEST_KEK"]).read().strip())
data = sys.stdin.buffer.read()
if os.environ.get("TEST_REVOKED"):
    sys.exit("AccessDeniedException: key disabled")
op = sys.argv[1]
sys.stdout.buffer.write(aes_key_wrap(kek, data) if op == "wrap" else aes_key_unwrap(kek, data))
"""


def test_a_kms_command_wraps_and_revoking_it_stops_unsealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "kms.py"
    script.write_text(KMS)
    kek = tmp_path / "kek"
    sealing.generate_key_file(kek)
    monkeypatch.setenv("TEST_KEK", str(kek))
    kms = sealing.CommandKey("aws-kms:alias/neura-weights", [sys.executable, str(script)])
    blob = sealed(tmp_path, b"policy weights", kms)
    assert opened(blob, kms) == b"policy weights"
    monkeypatch.setenv("TEST_REVOKED", "1")
    with pytest.raises(sealing.KeyUnavailable, match="AccessDenied"):
        opened(blob, kms)
    missing = sealing.CommandKey("k", [str(tmp_path / "no-such-command")])
    with pytest.raises(sealing.KeyUnavailable):
        missing.wrap(b"\x00" * 32)


def test_the_cli_seals_a_checkpoint_into_a_source_layout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    checkpoint = tmp_path / "checkpoint"
    (checkpoint / "sub").mkdir(parents=True)
    (checkpoint / "model.safetensors").write_bytes(os.urandom(5000))
    (checkpoint / "sub" / "config.json").write_bytes(b"{}")
    kek = tmp_path / "kek"
    assert cli.main(["key-file", "--out", str(kek)]) == 0
    assert cli.main(["key-file", "--out", str(kek)]) == 2
    out = tmp_path / "sealed"
    assert cli.main(["seal", str(checkpoint), "--out", str(out), "--key-file", str(kek)]) == 0
    printed = capsys.readouterr().out
    assert "path: model.safetensors" in printed
    assert "path: sub/config.json" in printed
    key = sealing.FileKey(kek)
    for path in ("model.safetensors", "sub/config.json"):
        data = (checkpoint / path).read_bytes()
        blob = (out / "sha256" / hashlib.sha256(data).hexdigest()).read_bytes()
        assert opened(blob, key) == data
