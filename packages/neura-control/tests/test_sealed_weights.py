"""Neura's registry of sealed weights (design §18.2): it accepts only files sealed for the digest
they are stored under, and holds nothing it could open."""

from __future__ import annotations

import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import AppFactory, ConformanceClient
from neura_contracts import sealing

Api = tuple[ConformanceClient, str]


def project(client: ConformanceClient, org: str) -> str:
    response = client.post(
        f"/v1/organizations/{org}/projects", {"spec": {"name": "p", "display_name": "P"}}
    )
    project_id: str = response.json()["metadata"]["id"]
    return project_id


def sealed(tmp_path: Path, data: bytes) -> tuple[str, bytes, sealing.FileKey]:
    (tmp_path / "plain").write_bytes(data)
    sealing.generate_key_file(tmp_path / "kek")
    key = sealing.FileKey(tmp_path / "kek")
    out = io.BytesIO()
    sealing.seal_file(tmp_path / "plain", out, key, chunk_bytes=1024)
    return f"sha256:{hashlib.sha256(data).hexdigest()}", out.getvalue(), key


def test_sealed_weights_are_stored_listed_and_removed(make_api: AppFactory, tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    client, org = make_api(settings_overrides={"sealed_weights_dir": registry})
    project_id = project(client, org)
    data = os.urandom(5000)
    digest, blob, key = sealed(tmp_path, data)
    path = f"/v1/projects/{project_id}/sealed-weights/{digest}"
    created = client.request("PUT", path, content=blob)
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["key_id"] == key.key_id
    assert (record["plaintext_bytes"], record["sealed_bytes"]) == (5000, len(blob))
    assert record["sealed_sha256"] == hashlib.sha256(blob).hexdigest()
    stored = next(registry.rglob(digest.removeprefix("sha256:")))
    assert stored.read_bytes() == blob
    assert data not in stored.read_bytes()
    assert created.headers["Location"] == path
    replaced = client.request("PUT", path, content=blob)
    assert replaced.status_code == 200
    assert replaced.json()["version"] == "2"
    listed = client.get(f"/v1/projects/{project_id}/sealed-weights").json()["items"]
    assert [w["digest"] for w in listed] == [digest]
    assert client.get(path).json()["digest"] == digest
    audit = client.get(f"/v1/projects/{project_id}/audit-records").json()["items"]
    assert any(r["action"] == "sealed_weights.upload" and r["target"] == digest for r in audit)
    assert client.delete(path, version=None).status_code == 428
    assert client.delete(path, version="1").status_code == 412
    assert client.delete(path, version="2").status_code == 204
    assert client.get(path).status_code == 404
    assert not stored.exists()


def test_only_complete_sealed_files_for_their_digest_are_accepted(
    make_api: AppFactory, tmp_path: Path
) -> None:
    client, org = make_api(settings_overrides={"sealed_weights_dir": tmp_path / "registry"})
    project_id = project(client, org)
    data = os.urandom(3000)
    digest, blob, _ = sealed(tmp_path, data)
    path = f"/v1/projects/{project_id}/sealed-weights/{digest}"
    other = f"/v1/projects/{project_id}/sealed-weights/sha256:{'0' * 64}"
    forged_length = sealing.MAGIC + (1 << 31).to_bytes(4, "big") + b"{}"
    for target, body, reason in (
        (path, data, "not a sealed file"),
        (other, blob, "sealed for"),
        (path, blob[:-1], "bytes, a sealed file"),
        (path, blob + b"x", "bytes, a sealed file"),
        (path, blob[:20], "truncated"),
        (path, forged_length, "header length"),
    ):
        refused = client.request("PUT", target, content=body)
        assert refused.status_code == 422, (reason, refused.text)
        assert reason in refused.json()["detail"]
    wrong_type = client.request(
        "PUT", path, content=blob, content_type="application/json", validate=False
    )
    assert wrong_type.status_code == 422
    assert client.request("PUT", f"/v1/projects/{project_id}/sealed-weights/nope", content=blob)
    assert client.get(f"/v1/projects/{project_id}/sealed-weights").json()["items"] == []
    assert not list((tmp_path / "registry" / ".incoming").iterdir())


def test_the_registry_is_off_unless_configured(api: Api, tmp_path: Path) -> None:
    client, org = api
    digest, blob, _ = sealed(tmp_path, b"weights")
    response = client.request(
        "PUT", f"/v1/projects/{project(client, org)}/sealed-weights/{digest}", content=blob
    )
    assert response.status_code == 404
    assert "own store" in response.json()["detail"]


def test_a_failed_change_leaves_the_stored_file_as_its_record_says(
    make_api: AppFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file changes only after its record is committed: a failure before the commit leaves
    the file as it was, under the record that describes it."""
    from neura_control.api.scope import Ctx

    registry = tmp_path / "registry"
    client, org = make_api(settings_overrides={"sealed_weights_dir": registry})
    project_id = project(client, org)
    data = os.urandom(4000)
    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    digest, first, _ = sealed(tmp_path / "first", data)
    _, second, _ = sealed(tmp_path / "second", data)  # the same weights, sealed with another key
    path = f"/v1/projects/{project_id}/sealed-weights/{digest}"
    assert client.request("PUT", path, content=first).status_code == 201
    stored = next(registry.rglob(digest.removeprefix("sha256:")))

    async def failing(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected before the commit")

    monkeypatch.setattr(Ctx, "record", failing)
    assert client.request("PUT", path, content=second).status_code == 500
    assert stored.read_bytes() == first  # not replaced under the first file's record
    assert client.delete(path, version="1").status_code == 500
    assert stored.read_bytes() == first  # not removed from under its record
    monkeypatch.undo()
    assert client.get(path).json()["sealed_sha256"] == hashlib.sha256(first).hexdigest()
    assert not list((registry / ".incoming").iterdir())


def test_concurrent_uploads_of_one_digest_leave_the_file_its_record_names(
    make_api: AppFactory, tmp_path: Path
) -> None:
    registry = tmp_path / "registry"
    client, org = make_api(settings_overrides={"sealed_weights_dir": registry})
    project_id = project(client, org)
    data = os.urandom(4000)
    blobs = []
    for name in ("a", "b", "c", "d"):
        (tmp_path / name).mkdir()
        digest, blob, _ = sealed(tmp_path / name, data)
        blobs.append(blob)
    path = f"/v1/projects/{project_id}/sealed-weights/{digest}"
    barrier = threading.Barrier(len(blobs))

    def upload(blob: bytes) -> int:
        barrier.wait()
        return client.request("PUT", path, content=blob).status_code

    with ThreadPoolExecutor(len(blobs)) as pool:
        assert sorted(pool.map(upload, blobs)) == [200, 200, 200, 201]
    stored = next(registry.rglob(digest.removeprefix("sha256:"))).read_bytes()
    assert client.get(path).json()["sealed_sha256"] == hashlib.sha256(stored).hexdigest()
    assert not list((registry / ".incoming").iterdir())


def test_opening_the_registry_clears_abandoned_uploads(
    make_api: AppFactory, tmp_path: Path
) -> None:
    incoming = tmp_path / "registry" / ".incoming"
    incoming.mkdir(parents=True)
    abandoned, current = incoming / "tmp-abandoned", incoming / "tmp-current"
    abandoned.write_bytes(b"part of an upload a stopped process left")
    current.write_bytes(b"an upload another replica is receiving")
    hours_ago = time.time() - 2 * 3600
    os.utime(abandoned, (hours_ago, hours_ago))
    make_api(settings_overrides={"sealed_weights_dir": tmp_path / "registry"})
    assert not abandoned.exists()
    assert current.exists()
