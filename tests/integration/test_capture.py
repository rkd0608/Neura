"""Episode upload from a robot's spool to the site's capture service over gRPC: authenticated,
only for revisions that capture, and kept on the robot until the site has it."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

import grpc
import pytest

from neura_runtime.capture.spool import READY, Spool
from neura_runtime.capture.testing import synthetic_episode
from neura_runtime.capture.uploader import Uploader
from neura_runtime.grpc_client import RobotCredentials
from neura_server.capture.service import CaptureServer
from neura_server.capture.store import EpisodeStore
from neura_server.config import DeviceConfig, DeviceRegistry, token_digest

TOKEN = "robot-one-token"


def registry() -> DeviceRegistry:
    return DeviceRegistry([DeviceConfig(robot_id="rbt_1", token_sha256=token_digest(TOKEN))])


def spooled(tmp_path: Path, make_binding: Any, **options: Any) -> Spool:
    """A robot spool holding one finished episode."""
    spool = Spool(tmp_path / "spool", max_bytes=1 << 30)
    episode = synthetic_episode(tmp_path / "made", make_binding(), image_stride=4, **options)
    shutil.move(str(episode.directory), spool.directory / READY / episode.episode_id)
    return spool


async def serve(store: EpisodeStore, allowed: set[tuple[str, str]]) -> CaptureServer:
    server = CaptureServer(
        store,
        "127.0.0.1:0",
        registry=registry(),
        tls=None,
        allowed=lambda robot, revision: (robot, revision) in allowed,
    )
    await server.start()
    return server


def uploader(spool: Spool, port: int, token: str = TOKEN) -> Uploader:
    credentials = RobotCredentials(token=token)
    return Uploader(
        spool,
        f"127.0.0.1:{port}",
        credentials.sync_channel,
        metadata=credentials.metadata(),
        label_grace_s=0,
    )


async def drain(up: Uploader) -> bool:
    channel = up._channel(up.target)
    try:
        from neura.v1 import capture_pb2_grpc

        return await asyncio.to_thread(up.drain, capture_pb2_grpc.CaptureStub(channel))
    finally:
        channel.close()


async def test_a_robot_uploads_its_episodes_once(tmp_path: Path, make_binding: Any) -> None:
    store = EpisodeStore(tmp_path / "site")
    stored: list[str] = []
    server = await serve(store, {("rbt_1", "rev_1")})
    server.servicer.on_stored = lambda record: stored.append(record["episode_id"])
    spool = spooled(tmp_path, make_binding, outcome="success")
    try:
        (episode,) = spool.ready()
        assert await drain(uploader(spool, server.port or 0))
        assert spool.ready() == []  # deleted on the robot once the site had it
        assert stored == [episode.name]
        assert store.get(episode.name)["outcome"] == "success"
        assert server.servicer.counters == {"stored": 1}
    finally:
        await server.stop()
        spool.stop()


async def test_uploads_are_refused_without_credentials_or_capture(
    tmp_path: Path, make_binding: Any
) -> None:
    store = EpisodeStore(tmp_path / "site")
    server = await serve(store, set())  # no deployment captures for this robot
    spool = spooled(tmp_path, make_binding)
    try:
        with pytest.raises(grpc.RpcError) as refused:
            await drain(uploader(spool, server.port or 0))
        assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
        assert "CAPTURE_NOT_ENABLED" in (refused.value.details() or "")
        with pytest.raises(grpc.RpcError) as unknown:
            await drain(uploader(spool, server.port or 0, token="someone-else"))
        assert unknown.value.code() is grpc.StatusCode.UNAUTHENTICATED
        assert len(spool.ready()) == 1  # kept on the robot
        assert store.summary()["episodes"] == 0
    finally:
        await server.stop()
        spool.stop()


async def test_one_robot_cannot_upload_as_another(tmp_path: Path, make_binding: Any) -> None:
    store = EpisodeStore(tmp_path / "site")
    server = await serve(store, {("rbt_1", "rev_1"), ("rbt_2", "rev_1")})
    spool = spooled(tmp_path, make_binding, robot_id="rbt_2")  # recorded as rbt_2
    try:
        with pytest.raises(grpc.RpcError) as refused:
            await drain(uploader(spool, server.port or 0))  # sent with rbt_1's credentials
        assert refused.value.code() is grpc.StatusCode.PERMISSION_DENIED
        assert "CAPTURE_ROBOT_MISMATCH" in (refused.value.details() or "")
    finally:
        await server.stop()
        spool.stop()
