"""Shadow serving with real policy workers (design §28.3): two ACT checkpoints (real architecture,
different random weights), each loaded by its own worker process on CPU. The base serves a
simulated robot over gRPC; the candidate shadows it. The candidate's chunks are compared and never
sent, and removing the shadow stops its worker and leaves the robot's alone."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("lerobot", reason="needs the models group: uv sync --group models")

from neura_bench.deployments import synthetic_act
from neura_runtime.runtime import RuntimeState
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
    ShadowConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer
from neura_sim.grpc_robots import SimulatedRobotProcess


async def test_a_candidate_worker_shadows_a_served_robot(tmp_path: Path) -> None:
    base = synthetic_act(tmp_path / "base", size="tiny", seed=0)
    candidate = synthetic_act(tmp_path / "candidate", size="tiny", seed=1)
    assert base.checkpoint is not None
    assert candidate.checkpoint is not None
    assert candidate.binding.artifact_digest != base.binding.artifact_digest
    serving = ServingConfig(backend="workers", max_batch=2, worker_start_timeout_s=120)
    deployment = DeploymentConfig(
        revision_id="rev_base",
        artifact=base.artifact,
        robot_profile=base.robot_profile,
        execution_profile=base.execution_profile,
        robots=["robot-0"],
        checkpoint=base.checkpoint,
    )
    config = GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[DeviceConfig(robot_id="robot-0", token_sha256=token_digest("token-robot-0"))],
        deployments=[deployment],
        shadows=[
            ShadowConfig(
                revision_id="rev_candidate",
                of_revision_id="rev_base",
                artifact=candidate.artifact,
                robot_profile=candidate.robot_profile,
                execution_profile=candidate.execution_profile,
                robots=["robot-0"],
                checkpoint=candidate.checkpoint,
            )
        ],
        serving=serving,
    )
    server = GatewayServer(config)
    await server.start()
    robot = SimulatedRobotProcess(
        base.binding, "robot-0", "rev_base", f"127.0.0.1:{server.bound_port}", "token-robot-0"
    )
    stop = asyncio.Event()
    task = asyncio.create_task(robot.connection.run(stop))
    try:
        # Robots are refused while workers start (WORKER_UNAVAILABLE): count from the first
        # comparison, when both workers are up.
        started: dict[str, int] | None = None
        for _ in range(2400):
            status = server.status()
            shadow = status["shadows"]["rev_candidate"]
            assert shadow["worker"]["state"] != "failed", shadow["worker"]
            if started is None and shadow["compared"] >= 1:
                started = dict(status["sessions"][0]["rejections"])
            if shadow["compared"] >= 5 and robot.runtime.dispatched > 60:
                break
            await asyncio.sleep(0.05)
        status = server.status()
        shadow = status["shadows"]["rev_candidate"]
        assert shadow["compared"] >= 5, shadow
        # Its own worker, with its own weights; never a robot's batch group.
        assert shadow["worker"]["worker"]["adapter"] == "lerobot-act"
        assert list(status["groups"]) == [next(iter(server.core.groups))]
        assert shadow["invalid_outputs"] == shadow["failed"] == 0
        assert shadow["divergence"]["mean"] > 0  # different weights, different actions
        # The robot is served by the base as if nothing shadowed it.
        assert robot.runtime.state is RuntimeState.RUNNING, robot.status()
        assert robot.runtime.fault is None
        (session,) = status["sessions"]
        assert session["revision_id"] == "rev_base"
        assert session["rejections"] == started  # none while the candidate shadowed it

        # Without the shadow, its worker stops; the robot's keeps serving.
        server.reload(config.model_copy(update={"shadows": []}))
        served = robot.runtime.counters["chunks_accepted"]
        for _ in range(400):
            if robot.runtime.counters["chunks_accepted"] > served + 2:
                break
            await asyncio.sleep(0.05)
        assert server.status()["shadows"] == {}
        assert robot.runtime.counters["chunks_accepted"] > served + 2
        assert robot.runtime.fault is None
    finally:
        stop.set()
        await task
        await server.stop()


async def test_a_candidate_that_cannot_load_never_touches_the_robot(tmp_path: Path) -> None:
    base = synthetic_act(tmp_path / "base", size="tiny", seed=0)
    candidate = synthetic_act(tmp_path / "candidate", size="tiny", seed=1)
    assert base.checkpoint is not None
    assert candidate.checkpoint is not None
    (candidate.checkpoint / "model.safetensors").write_bytes(b"not a model")  # damaged weights
    config = GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[DeviceConfig(robot_id="robot-0", token_sha256=token_digest("token-robot-0"))],
        deployments=[
            DeploymentConfig(
                revision_id="rev_base",
                artifact=base.artifact,
                robot_profile=base.robot_profile,
                execution_profile=base.execution_profile,
                robots=["robot-0"],
                checkpoint=base.checkpoint,
            )
        ],
        shadows=[
            ShadowConfig(
                revision_id="rev_candidate",
                of_revision_id="rev_base",
                artifact=candidate.artifact,
                robot_profile=candidate.robot_profile,
                execution_profile=candidate.execution_profile,
                robots=["robot-0"],
                checkpoint=candidate.checkpoint,
            )
        ],
        serving=ServingConfig(backend="workers", max_batch=2, worker_start_timeout_s=120),
    )
    server = GatewayServer(config)
    await server.start()
    robot = SimulatedRobotProcess(
        base.binding, "robot-0", "rev_base", f"127.0.0.1:{server.bound_port}", "token-robot-0"
    )
    stop = asyncio.Event()
    task = asyncio.create_task(robot.connection.run(stop))
    try:
        for _ in range(2400):
            shadow = server.status()["shadows"]["rev_candidate"]
            if shadow["worker"]["state"] == "failed" and robot.runtime.dispatched > 60:
                break
            await asyncio.sleep(0.05)
        shadow = server.status()["shadows"]["rev_candidate"]
        assert shadow["worker"]["state"] == "failed"
        assert shadow["worker"]["last_error"]["code"]  # why, for the evaluation's report
        assert shadow["compared"] == shadow["failed"] == 0  # never asked, so never failed
        assert robot.runtime.state is RuntimeState.RUNNING, robot.status()
        assert robot.runtime.fault is None
    finally:
        stop.set()
        await task
        await server.stop()
