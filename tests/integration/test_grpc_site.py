"""End to end over real gRPC: a gateway process core, its dispatcher and simulated robots."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from neura_runtime.runtime import RuntimeState
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer
from neura_sim.grpc_robots import SimulatedRobotProcess, load_binding

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "contracts"
ARTIFACT = EXAMPLES / "act-so101.artifact.yaml"
ROBOT = EXAMPLES / "so101.robot.yaml"
PROFILE = EXAMPLES / "act-so101-30hz.profile.yaml"


def gateway_config(robots: list[str]) -> GatewayConfig:
    return GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[DeviceConfig(robot_id=r, token_sha256=token_digest(f"token-{r}")) for r in robots],
        deployments=[
            DeploymentConfig(
                revision_id="rev_test",
                artifact=ARTIFACT,
                robot_profile=ROBOT,
                execution_profile=PROFILE,
                robots=robots,
            )
        ],
        serving=ServingConfig(backend="simulated", simulate_latency=True),
    )


async def test_robots_run_through_a_live_gateway() -> None:
    names = ["robot-0", "robot-1", "robot-2"]
    server = GatewayServer(gateway_config(names))
    await server.start()
    target = f"127.0.0.1:{server.bound_port}"
    binding = load_binding(ARTIFACT, ROBOT, PROFILE)
    robots = [SimulatedRobotProcess(binding, n, "rev_test", target, f"token-{n}") for n in names]
    intruder = SimulatedRobotProcess(binding, "robot-0", "rev_test", target, "wrong-token")
    stop = asyncio.Event()
    tasks = [asyncio.create_task(r.connection.run(stop)) for r in [*robots, intruder]]
    try:
        await asyncio.sleep(4.0)
        status = server.status()
        states = {r.robot_id: r.runtime.state for r in robots}
        dispatched = {r.robot_id: r.runtime.dispatched for r in robots}
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        await server.stop()

    assert set(states.values()) == {RuntimeState.RUNNING}, states
    assert all(n > 60 for n in dispatched.values()), dispatched
    assert all(r.runtime.fault is None for r in robots)
    assert len(status["sessions"]) == 3
    group = next(iter(status["groups"].values()))
    assert group["dispatched_requests"] >= 9
    # A wrong token never gets a session.
    assert intruder.runtime.session_id is None
    assert intruder.runtime.dispatched == 0


async def test_gateway_closes_sessions_when_robots_stop() -> None:
    server = GatewayServer(gateway_config(["robot-0"]))
    await server.start()
    binding = load_binding(ARTIFACT, ROBOT, PROFILE)
    robot = SimulatedRobotProcess(
        binding, "robot-0", "rev_test", f"127.0.0.1:{server.bound_port}", "token-robot-0"
    )
    stop = asyncio.Event()
    task = asyncio.create_task(robot.connection.run(stop))
    try:
        await asyncio.sleep(1.0)
        assert len(server.core.sessions) == 1
        stop.set()
        await task
        assert server.core.sessions == {}  # the client closed its session on shutdown
    finally:
        stop.set()
        await server.stop()


@pytest.mark.parametrize("bad", ["", "Bearer "])
def test_registry_rejects_empty_tokens(bad: str) -> None:
    from neura_server.config import DeviceRegistry

    registry = DeviceRegistry([DeviceConfig(robot_id="r", token_sha256=token_digest("t"))])
    assert registry.authenticate(bad.removeprefix("Bearer ")) is None
    assert registry.authenticate("t") == "r"
