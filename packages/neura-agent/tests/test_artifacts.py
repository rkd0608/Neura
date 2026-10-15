"""The site's artifact store: imports, verification, sources and checkouts (design §13, §18.1)."""

from __future__ import annotations

import hashlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from neura_agent.__main__ import main as agent_main
from neura_agent.artifacts import ArtifactError, ArtifactStore, load_keys
from neura_agent.settings import ArtifactKey
from neura_contracts import ModelArtifact, ReasonCode, parse_document, sealing

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"


def checkpoint(directory: Path) -> dict[str, bytes]:
    files = {
        "config.json": b'{"type": "act"}',
        "model.safetensors": b"\x00weights" * 100,
        "processors/policy_preprocessor.json": b"{}",
    }
    for name, data in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(data)
    return files


def artifact_for(files: dict[str, bytes]) -> ModelArtifact:
    document: dict[str, Any] = yaml.safe_load(
        (EXAMPLES / "act-so101.artifact.yaml").read_text(encoding="utf-8")
    )
    document["spec"]["weights"] = [
        {
            "path": name,
            "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
            "size_bytes": len(data),
        }
        for name, data in files.items()
    ]
    artifact = parse_document(document)
    assert isinstance(artifact, ModelArtifact)
    return artifact


def test_imported_files_are_checked_out_read_only(tmp_path: Path) -> None:
    files = checkpoint(tmp_path / "ckpt")
    store = ArtifactStore(tmp_path / "store")
    imported = store.import_directory(tmp_path / "ckpt")
    assert sorted(i.path for i in imported) == sorted(files)
    artifact = artifact_for(files)
    out = store.checkout(artifact)
    for name, data in files.items():
        assert (out / name).read_bytes() == data
        assert (out / name).stat().st_mode & 0o222 == 0
    assert store.checkout(artifact) == out  # idempotent


def test_missing_weights_name_the_files_and_the_fix(tmp_path: Path) -> None:
    files = checkpoint(tmp_path / "ckpt")
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactError) as raised:
        store.checkout(artifact_for(files))
    assert raised.value.code is ReasonCode.AGENT_ARTIFACT_UNAVAILABLE
    assert "model.safetensors" in str(raised.value)
    assert "import-weights" in str(raised.value)


def test_a_file_that_does_not_match_its_digest_is_refused(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"tampered")
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactError) as raised:
        store.add_file(tmp_path / "f", expected="sha256:" + "0" * 64)
    assert raised.value.code is ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH
    assert not store.has("sha256:" + hashlib.sha256(b"tampered").hexdigest())


def test_weights_are_fetched_from_a_directory_source(tmp_path: Path) -> None:
    files = checkpoint(tmp_path / "ckpt")
    mirror = tmp_path / "mirror" / "sha256"
    mirror.mkdir(parents=True)
    for data in files.values():
        (mirror / hashlib.sha256(data).hexdigest()).write_bytes(data)
    store = ArtifactStore(tmp_path / "store", [str(tmp_path / "mirror")])
    out = store.checkout(artifact_for(files))
    assert (out / "config.json").read_bytes() == files["config.json"]


@pytest.fixture
def http_mirror(tmp_path: Path) -> Iterator[tuple[str, Path, list[str]]]:
    root = tmp_path / "web"
    (root / "sha256").mkdir(parents=True)
    seen: list[str] = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization", ""))
            super().do_GET()

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_weights_are_fetched_from_an_http_source_and_verified(
    tmp_path: Path, http_mirror: tuple[str, Path, list[str]]
) -> None:
    url, root, seen = http_mirror
    files = checkpoint(tmp_path / "ckpt")
    for data in files.values():
        (root / "sha256" / hashlib.sha256(data).hexdigest()).write_bytes(data)
    store = ArtifactStore(tmp_path / "store", [url], token="registry-token")
    out = store.checkout(artifact_for(files))
    assert (out / "model.safetensors").read_bytes() == files["model.safetensors"]
    assert set(seen) == {"Bearer registry-token"}

    # A source serving the wrong bytes under a digest is refused.
    bad = {"config.json": b"original"}
    (root / "sha256" / hashlib.sha256(b"original").hexdigest()).write_bytes(b"swapped!")
    with pytest.raises(ArtifactError) as raised:
        ArtifactStore(tmp_path / "store2", [url]).checkout(artifact_for(bad))
    assert raised.value.code is ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH


def sealed_mirror(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, sealing.FileKey]:
    """The checkpoint sealed with a customer key (neura-contracts seal) in a source layout."""
    sealing.generate_key_file(tmp_path / "kek")
    key = sealing.FileKey(tmp_path / "kek")
    sealing.seal_directory(tmp_path / "ckpt", tmp_path / "sealed", key)
    for data in files.values():
        assert (
            data not in (tmp_path / "sealed/sha256" / hashlib.sha256(data).hexdigest()).read_bytes()
        )
    return tmp_path / "sealed", key


def test_sealed_weights_are_opened_with_the_sites_key(tmp_path: Path) -> None:
    files = checkpoint(tmp_path / "ckpt")
    mirror, key = sealed_mirror(tmp_path, files)
    store = ArtifactStore(tmp_path / "store", [str(mirror)], keys={key.key_id: key})
    out = store.checkout(artifact_for(files))
    for name, data in files.items():
        assert (out / name).read_bytes() == data


def test_sealed_weights_need_the_key_and_untampered_bytes(tmp_path: Path) -> None:
    files = checkpoint(tmp_path / "ckpt")
    mirror, key = sealed_mirror(tmp_path, files)
    with pytest.raises(ArtifactError) as no_key:
        ArtifactStore(tmp_path / "store", [str(mirror)]).checkout(artifact_for(files))
    assert no_key.value.code is ReasonCode.AGENT_ARTIFACT_KEY_UNAVAILABLE
    assert key.key_id in str(no_key.value)
    blob = mirror / "sha256" / hashlib.sha256(files["model.safetensors"]).hexdigest()
    data = bytearray(blob.read_bytes())
    data[-20] ^= 1
    blob.write_bytes(bytes(data))
    store = ArtifactStore(tmp_path / "store", [str(mirror)], keys={key.key_id: key})
    with pytest.raises(ArtifactError) as tampered:
        store.checkout(artifact_for(files))
    assert tampered.value.code is ReasonCode.AGENT_ARTIFACT_DIGEST_MISMATCH
    assert not store.has("sha256:" + hashlib.sha256(files["model.safetensors"]).hexdigest())
    assert not list((tmp_path / "store" / "incoming").iterdir())  # nothing partial left


def test_sealed_weights_are_fetched_over_https(
    tmp_path: Path, http_mirror: tuple[str, Path, list[str]]
) -> None:
    url, root, _ = http_mirror
    files = checkpoint(tmp_path / "ckpt")
    mirror, key = sealed_mirror(tmp_path, files)
    for blob in (mirror / "sha256").iterdir():
        (root / "sha256" / blob.name).write_bytes(blob.read_bytes())
    store = ArtifactStore(tmp_path / "store", [url], keys={key.key_id: key})
    out = store.checkout(artifact_for(files))
    assert (out / "model.safetensors").read_bytes() == files["model.safetensors"]


def test_artifact_keys_load_from_settings(tmp_path: Path) -> None:
    sealing.generate_key_file(tmp_path / "kek")
    file_key = sealing.FileKey(tmp_path / "kek")
    keys = load_keys(
        [
            ArtifactKey(file=tmp_path / "kek"),
            ArtifactKey(key_id="vault:transit/neura", command=["neura-vault-key"]),
        ]
    )
    assert sorted(keys) == sorted([file_key.key_id, "vault:transit/neura"])
    with pytest.raises(ValueError, match="is key"):
        load_keys([ArtifactKey(file=tmp_path / "kek", key_id="file:0000000000000000")])
    with pytest.raises(ValueError, match="needs its key_id"):
        ArtifactKey(command=["x"])
    with pytest.raises(ValueError, match="either file or command"):
        ArtifactKey()


def test_unused_checkouts_are_pruned_but_imports_kept(tmp_path: Path) -> None:
    files = checkpoint(tmp_path / "ckpt")
    store = ArtifactStore(tmp_path / "store")
    store.import_directory(tmp_path / "ckpt")
    artifact = artifact_for(files)
    out = store.checkout(artifact)
    store.prune_checkouts([])
    assert not out.exists()
    assert store.missing(artifact) == []


def test_import_weights_cli_checks_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("NEURA_AGENT_STATE_DIR", str(tmp_path / "agent"))
    files = checkpoint(tmp_path / "ckpt")
    manifest = tmp_path / "artifact.json"
    document = artifact_for(files).model_dump(mode="json", by_alias=True, exclude_none=True)
    manifest.write_text(yaml.safe_dump(document))
    assert agent_main(["import-weights", str(tmp_path / "ckpt"), "--artifact", str(manifest)]) == 0
    assert "imported the 3 files" in capsys.readouterr().out

    (tmp_path / "ckpt" / "config.json").write_bytes(b"changed")
    assert agent_main(["import-weights", str(tmp_path / "ckpt"), "--artifact", str(manifest)]) == 1
    assert "refused" in capsys.readouterr().err
