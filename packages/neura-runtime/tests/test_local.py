"""The runtime without a gateway: an ``@neura.policy`` class served through ``LocalConnection``,
in this process and over the network (``neura-robot serve``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from neura.v1 import worker_pb2
from neura_contracts import ReasonCode
from neura_runtime import remote
from neura_runtime.authored import AuthoredPolicy
from neura_runtime.binding import ExecutionBinding, load_binding
from neura_runtime.invariants import InvariantMonitor, watch_robot
from neura_runtime.local import LocalConnection
from neura_runtime.policy import InProcessPolicy, PolicyClient
from neura_runtime.runtime import RobotRuntime, RuntimeState
from neura_runtime.session import SessionClient, SessionConfig
from neura_runtime.testing import FakeController, FakeSensors

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"

# Holds still until a file named "fail" appears beside it, then fails every call.
POLICY = """
from pathlib import Path

import numpy as np
import neura_contracts.authoring as neura


@neura.policy(chunk_length={chunk_length}, cameras={{"front": (640, 480), "wrist": (640, 480)}})
class Still:
    def act(self, observation):
        if (Path(__file__).parent / "fail").exists():
            raise RuntimeError("the model fell over")
        return np.zeros(({chunk_length}, {action_dim}), dtype=np.float32)
"""


def now_us() -> int:
    return time.monotonic_ns() // 1000


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


@asynccontextmanager
async def reached(
    how: str, policy: AuthoredPolicy, binding: ExecutionBinding
) -> AsyncIterator[PolicyClient]:
    if how == "in-process":
        yield InProcessPolicy(policy)
        return
    description = worker_pb2.WorkerDescription(chunk_length=policy.declaration.chunk_length)
    server, port = await remote.serve(
        policy, "127.0.0.1:0", description=description, token="secret"
    )
    client = remote.RemotePolicy(f"127.0.0.1:{port}", token="secret")
    try:
        await client.check(binding)
        yield client
    finally:
        await client.close()
        await server.stop(grace=None)


@pytest.mark.parametrize("how", ["in-process", "served"])
async def test_a_policy_drives_the_runtime_and_its_failures_stop_it(
    how: str, tmp_path: Path
) -> None:
    binding = load_binding(
        EXAMPLES / "act-so101.artifact.yaml",
        EXAMPLES / "so101.robot.yaml",
        EXAMPLES / "act-so101-30hz.profile.yaml",
    )
    source = tmp_path / "still.py"
    source.write_text(
        POLICY.format(chunk_length=binding.chunk_length, action_dim=binding.action_dim)
    )
    controller = FakeController()
    runtime = RobotRuntime(binding, controller, FakeSensors(binding))
    client = SessionClient(runtime, SessionConfig(robot_id="arm-1", revision_id="rev_local"))
    async with reached(how, AuthoredPolicy.load(str(source)), binding) as policy:
        connection = LocalConnection(client, policy)
        monitor = InvariantMonitor(clock=now_us, gateway_observed=False)
        watch_robot(
            monitor,
            connection,
            "arm-1",
            client,
            lambda: controller.commands[-1] if controller.commands else None,
        )
        stop = asyncio.Event()
        serving = asyncio.create_task(connection.run(stop))
        try:
            await until(lambda: runtime.state is RuntimeState.READY)
            await asyncio.sleep(0.3)
            assert controller.commands == []  # nothing moves until armed

            runtime.arm(now_us())
            await until(lambda: runtime.state is RuntimeState.RUNNING and runtime.dispatched > 30)

            (tmp_path / "fail").touch()  # every request fails now: the robot runs dry and stops
            await until(lambda: runtime.state is RuntimeState.FAULTED)
            assert connection.counters[ReasonCode.WORKER_INFERENCE_FAILED.value] > 0
            commands = len(controller.commands)
            await asyncio.sleep(0.3)
            assert len(controller.commands) == commands
        finally:
            stop.set()
            await serving
    assert monitor.violations == 0, monitor.summary()
