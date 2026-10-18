"""The site's episodes through the agent's local API (design §28.2): listed, viewed frame by frame
and step by step, labeled and deleted by the site's operator, behind the operator credential.
The viewer page is static and loads only itself."""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from numpy.typing import NDArray

from neura_agent.app import create_app
from neura_agent.settings import AgentSettings
from neura_runtime.capture.episode import canonical
from neura_runtime.capture.testing import synthetic_episode
from neura_server.capture.store import EpisodeStore

TOKEN = "ntop_" + "v" * 43


class Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    def write(self, action: str, **fields: Any) -> None:
        self.records.append((action, fields))


def decode_png(data: bytes) -> NDArray[np.uint8]:
    """The pixels of an 8-bit RGB PNG with unfiltered rows (what the viewer writes)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position, idat, width, height = 8, b"", 0, 0
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        tag = data[position + 4 : position + 8]
        body = data[position + 8 : position + 8 + length]
        if tag == b"IHDR":
            width, height, depth, color = struct.unpack(">IIBB", body[:10])
            assert (depth, color) == (8, 2)
        elif tag == b"IDAT":
            idat += body
        position += 12 + length
    rows = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(height, 1 + width * 3)
    assert not rows[:, 0].any()  # filter type 0 on every row
    return rows[:, 1:].reshape(height, width, 3)


@pytest.fixture
def site(make_binding: Any, tmp_path: Path) -> tuple[TestClient, EpisodeStore, Audit, Any]:
    binding = make_binding()
    store = EpisodeStore(tmp_path / "episodes")
    episode = synthetic_episode(
        tmp_path / "recorded",
        binding,
        robot_id="arm-1",
        revision_id="rev_1",
        steps=24,
        image_stride=3,
        outcome="failure",
    )
    upload = store.begin(canonical(episode.manifest), robot_id="arm-1")
    for name in sorted(episode.manifest["files"]):
        upload.write(name, (episode.directory / name).read_bytes())
    assert upload.commit()
    audit = Audit()
    runtime = SimpleNamespace(
        core=SimpleNamespace(capture=SimpleNamespace(store=store), audit=audit)
    )
    settings = AgentSettings(
        state_dir=tmp_path / "agent",
        operator_token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
    )
    app = create_app(settings, runtime=runtime, start_probe=False)  # type: ignore[arg-type]
    return TestClient(app), store, audit, episode


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_episodes_need_the_operator_credential(site: Any) -> None:
    client, _, _, episode = site
    for path in (
        "/v1/local/episodes",
        f"/v1/local/episodes/{episode.episode_id}",
        f"/v1/local/episodes/{episode.episode_id}/steps",
        f"/v1/local/episodes/{episode.episode_id}/frames/front/0",
    ):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_an_episode_step_by_step_and_frame_by_frame(site: Any) -> None:
    client, store, _, episode = site
    listed = client.get("/v1/local/episodes", headers=AUTH).json()
    assert [r["episode_id"] for r in listed["items"]] == [episode.episode_id]
    steps = client.get(f"/v1/local/episodes/{episode.episode_id}/steps", headers=AUTH).json()
    recorded = episode.steps()
    assert steps["steps"] == len(recorded.t_us) == 24
    assert steps["command_names"] == episode.manifest["command_names"]
    assert len(steps["action"][0]) == len(steps["command_names"])
    cameras = {c["role"]: c for c in steps["cameras"]}
    assert set(cameras) == {c["role"] for c in episode.manifest["cameras"]}
    assert all(c["frames"] == (recorded.frame >= 0).sum() for c in cameras.values())
    # The frame shown at each step is the latest recorded by then.
    assert steps["frame"] == list(np.maximum.accumulate(recorded.frame))
    role = next(iter(cameras))
    response = client.get(f"/v1/local/episodes/{episode.episode_id}/frames/{role}/1", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"].startswith("private")
    stored = store.episode(episode.episode_id)
    expected = stored.frame(role, 1)
    if next(c for c in stored.manifest["cameras"] if c["role"] == role)["color_order"] == "bgr":
        expected = expected[..., ::-1]
    assert np.array_equal(decode_png(response.content), expected)
    for path in (f"frames/{role}/{cameras[role]['frames']}", "frames/nope/0", "frames/front/-1"):
        missing = client.get(f"/v1/local/episodes/{episode.episode_id}/{path}", headers=AUTH)
        assert missing.status_code == 404, path
    assert client.get("/v1/local/episodes/ep_NONE/steps", headers=AUTH).status_code == 404


def test_long_episodes_are_sent_in_part() -> None:
    from neura_agent.episode_viewer import steps_json
    from neura_runtime.capture.episode import Steps

    total = 5_001
    steps = Steps(
        t_us=np.arange(total, dtype=np.int64) * 33_333,
        action_index=np.arange(total, dtype=np.int64),
        action=np.zeros((total, 2)),
        state=np.full((total, 2), np.nan),
        frame=np.where(np.arange(total) % 5 == 0, np.arange(total) // 5, -1).astype(np.int64),
    )
    episode = SimpleNamespace(
        episode_id="ep_long", manifest={"command_names": ["a", "b"]}, steps=lambda: steps
    )
    sent = steps_json(episode, max_rows=2_000)  # type: ignore[arg-type]
    assert sent["every"] == 3
    assert len(sent["t_s"]) == len(sent["action"]) == len(sent["frame"]) <= 2_000
    assert sent["state"][0] == [None, None]  # missing readings are null, not NaN


def test_the_operator_labels_and_deletes_episodes(site: Any) -> None:
    client, store, audit, episode = site
    path = f"/v1/local/episodes/{episode.episode_id}"
    labeled = client.post(
        f"{path}/label",
        json={"outcome": "success", "note": "picked on retry", "operator": "ops@site"},
        headers=AUTH,
    )
    assert labeled.status_code == 200, labeled.text
    record = store.get(episode.episode_id)
    assert (record["outcome"], record["outcome_source"], record["outcome_note"]) == (
        "success",
        "operator",
        "picked on retry",
    )
    assert ("episode_labeled", {"episode": episode.episode_id, "outcome": "success",
            "operator": "ops@site"}) in audit.records  # fmt: skip
    bad = client.post(f"{path}/label", json={"outcome": "great", "operator": "x"}, headers=AUTH)
    assert bad.status_code == 422
    assert client.delete(path, headers=AUTH).status_code == 200
    assert client.get(path, headers=AUTH).status_code == 404
    assert ("episode_deleted", {"episode": episode.episode_id}) in audit.records


def test_the_viewer_page_loads_only_itself(site: Any) -> None:
    client, _, _, _ = site
    page = client.get("/episodes")  # no credential: the page holds no data
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    policy = page.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert page.headers["x-content-type-options"] == "nosniff"
    assert '<script src="/episodes.js"' in page.text
    assert "<script>" not in page.text  # no inline script for the policy to allow
    script = client.get("/episodes.js")
    assert script.headers["content-type"].startswith("text/javascript")
    assert "innerHTML" not in script.text  # data is set as text, never parsed as HTML
    assert client.get("/episodes.css").headers["content-type"].startswith("text/css")
