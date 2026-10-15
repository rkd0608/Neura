"""A real model behind the gateway: a synthetic ACT checkpoint (real architecture, random weights)
imported into the site's artifact store, checked out, loaded by a policy worker process on CPU,
and serving a simulated robot over gRPC (design §12, §13)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")

from neura_agent.artifacts import ArtifactStore
from neura_bench.deployments import synthetic_act
from neura_runtime.runtime import RuntimeState
from neura_server import worker
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer
from neura_sim.grpc_robots import SimulatedRobotProcess


@pytest.fixture(scope="module")
def act(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("act")


async def test_act_serves_a_robot_from_the_artifact_store(tmp_path: Path, act: Path) -> None:
    files = synthetic_act(act, size="tiny")
    assert files.checkpoint is not None
    store = ArtifactStore(tmp_path / "store")
    store.import_directory(files.checkpoint)
    checkout = store.checkout(files.binding.artifact)

    config = GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[DeviceConfig(robot_id="robot-0", token_sha256=token_digest("token-robot-0"))],
        deployments=[
            DeploymentConfig(
                revision_id="rev_act",
                artifact=files.artifact,
                robot_profile=files.robot_profile,
                execution_profile=files.execution_profile,
                robots=["robot-0"],
                checkpoint=checkout,
            )
        ],
        serving=ServingConfig(backend="workers", max_batch=2, worker_start_timeout_s=120),
    )
    server = GatewayServer(config)
    await server.start()
    robot = SimulatedRobotProcess(
        files.binding, "robot-0", "rev_act", f"127.0.0.1:{server.bound_port}", "token-robot-0"
    )
    stop = asyncio.Event()
    task = asyncio.create_task(robot.connection.run(stop))
    try:
        for _ in range(1200):
            group = next(iter(server.status()["groups"].values()))
            if robot.runtime.state is RuntimeState.RUNNING and robot.runtime.dispatched > 30:
                break
            assert group["worker"]["state"] != "failed", group["worker"]
            await asyncio.sleep(0.05)
        status = server.status()
    finally:
        stop.set()
        await task
        await server.stop()
    group = next(iter(status["groups"].values()))
    assert group["worker"]["worker"]["adapter"] == "lerobot-act"
    assert group["worker"]["worker"]["parameters"] > 100_000
    assert robot.runtime.state is RuntimeState.RUNNING, robot.status()
    assert robot.runtime.fault is None
    assert status["sessions"][0]["chunks"] >= 1


def test_profile_mode_measures_act(act: Path, capsys: pytest.CaptureFixture[str]) -> None:
    files = synthetic_act(act / "profiled", size="tiny")
    assert files.checkpoint is not None
    code = worker.main(
        [
            "profile",
            "--artifact",
            str(files.artifact),
            "--robot-profile",
            str(files.robot_profile),
            "--execution-profile",
            str(files.execution_profile),
            "--checkpoint",
            str(files.checkpoint),
            "--device",
            "cpu",
            "--batch-sizes",
            "1,2",
            "--iterations",
            "5",
        ]
    )
    assert code == 0
    profile = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["profile"]
    assert profile["adapter"] == "lerobot-act"
    one, two = profile["points"]
    assert (one["batch_size"], two["batch_size"]) == (1, 2)
    assert one["p50_us"] > 0
