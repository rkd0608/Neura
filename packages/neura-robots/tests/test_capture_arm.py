"""Episode capture end to end on the emulated SO-101: the site's assignment tells the robot to
record; the robot records the task, the operator labels it, and the episode arrives in the
site's store, verified, while the arm is served as usual."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytest.importorskip("lerobot", reason="needs LeRobot (the lerobot or so101 extra, Python 3.12+)")
pytest.importorskip("neura_server", reason="episode capture needs Neura's site gateway")

from neura_contracts import RobotProfile
from neura_robots.emulator import SO101Emulator, write_calibration
from neura_robots.process import AssignedArm, OperatorInbox
from neura_robots.so101 import CameraConfig, So101Arm, So101Config
from neura_runtime.runtime import RuntimeState
from neura_server.capture.service import CaptureServer
from neura_server.capture.store import EpisodeStore
from neura_server.config import (
    CaptureConfig,
    DeploymentConfig,
    DeviceConfig,
    DeviceRegistry,
    GatewayConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"
TOKEN = "token-arm-1"


@pytest.fixture
async def site(tmp_path: Path) -> AsyncIterator[tuple[str, EpisodeStore]]:
    devices = [DeviceConfig(robot_id="arm-1", token_sha256=token_digest(TOKEN))]
    store = EpisodeStore(tmp_path / "site-episodes")
    capture = CaptureServer(
        store,
        "127.0.0.1:0",
        registry=DeviceRegistry(devices),
        tls=None,
        allowed=lambda robot, revision: (robot, revision) == ("arm-1", "rev_arm"),
    )
    await capture.start()
    gateway = GatewayServer(
        GatewayConfig(
            listen="127.0.0.1:0",
            status_listen=None,
            devices=devices,
            deployments=[
                DeploymentConfig(
                    revision_id="rev_arm",
                    artifact=EXAMPLES / "act-so101.artifact.yaml",
                    robot_profile=EXAMPLES / "so101.robot.yaml",
                    execution_profile=EXAMPLES / "act-so101-30hz.profile.yaml",
                    robots=["arm-1"],
                    capture=CaptureConfig(
                        mode="all", endpoint=f"127.0.0.1:{capture.port}", image_stride=3
                    ),
                )
            ],
            assignments={"arm-1": "rev_arm"},
            serving=ServingConfig(backend="simulated", simulate_latency=True),
        )
    )
    await gateway.start()
    yield f"127.0.0.1:{gateway.bound_port}", store
    await gateway.stop()
    await capture.stop()


async def until(condition: object, timeout: float, said: list[str]) -> None:
    deadline = time.monotonic() + timeout
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, said[-10:]
        await asyncio.sleep(0.05)


async def test_a_recorded_task_reaches_the_site_with_its_label(
    site: tuple[str, EpisodeStore], tmp_path: Path
) -> None:
    gateway, store = site
    with SO101Emulator(positions_deg={"shoulder_pan": 20.0}) as emulator:
        write_calibration(emulator, tmp_path / "calibration", "arm-1")
        config = So101Config(
            port=emulator.port,
            calibration_id="arm-1",
            calibration_dir=tmp_path / "calibration",
            cameras={"front": CameraConfig("synthetic"), "wrist": CameraConfig("synthetic")},
            lock_dir=tmp_path / "locks",
        )
        opened: list[So101Arm] = []

        def open_arm(profile: RobotProfile) -> So101Arm:
            arm = So101Arm(config, profile)
            arm.connect()
            opened.append(arm)
            return arm

        said: list[str] = []
        inbox = OperatorInbox()
        assigned = AssignedArm(
            open_arm,
            "arm-1",
            gateway=gateway,
            credentials=TOKEN,
            inbox=inbox,
            say=said.append,
            poll_seconds=0.2,
            capture_dir=tmp_path / "spool",
        )
        stop = asyncio.Event()
        task = asyncio.create_task(assigned.run(stop))
        try:
            await until(
                lambda: (
                    assigned.process is not None
                    and assigned.process.runtime.state is RuntimeState.READY
                ),
                10,
                said,
            )
            process = assigned.process
            assert process is not None
            assert process.capture is not None
            assert "arm-1: recording all episodes" in said
            inbox.submit("arm")
            await until(lambda: process.runtime.dispatched >= 30, 10, said)
            inbox.submit("outcome success placed on the first try")
            inbox.submit("stop")
            await until(lambda: store.summary()["episodes"] == 1, 20, said)
        finally:
            stop.set()
            await task
            for arm in opened:
                arm.close()
    (record,) = store.find()
    assert record["robot_id"] == "arm-1"
    assert record["revision_id"] == "rev_arm"
    assert (record["outcome"], record["outcome_source"]) == ("success", "operator")
    assert record["outcome_note"] == "placed on the first try"
    assert record["end_reason"] == "stopped"
    assert record["complete"] is True
    assert record["steps"] >= 30
    episode = store.episode(record["episode_id"])
    steps = episode.steps()
    assert steps.action.shape == (record["steps"], 6)
    assert (steps.frame >= 0).sum() == record["frames"] > 0
    assert episode.frame("front", 0).shape == (480, 640, 3)
    assert not any(line.startswith("unknown command") for line in said)
