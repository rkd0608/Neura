"""The runtime without a gateway: a policy in this process served through ``LocalConnection``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from neura_contracts import ReasonCode
from neura_runtime.binding import load_binding
from neura_runtime.invariants import InvariantMonitor, watch_robot
from neura_runtime.local import LocalConnection
from neura_runtime.policy import InProcessPolicy, ModelInputs, PolicyError
from neura_runtime.runtime import RobotRuntime, RuntimeState
from neura_runtime.session import SessionClient, SessionConfig
from neura_runtime.testing import FakeController, FakeSensors

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"


def now_us() -> int:
    return time.monotonic_ns() // 1000


async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.01)


async def test_an_in_process_policy_drives_the_runtime_and_its_failures_stop_it() -> None:
    binding = load_binding(
        EXAMPLES / "act-so101.artifact.yaml",
        EXAMPLES / "so101.robot.yaml",
        EXAMPLES / "act-so101-30hz.profile.yaml",
    )
    failing = False

    def policy(inputs: ModelInputs) -> NDArray[np.float32]:
        if failing:
            raise PolicyError("the model fell over")
        return np.zeros((binding.chunk_length, binding.action_dim), dtype=np.float32)

    controller = FakeController()
    runtime = RobotRuntime(binding, controller, FakeSensors(binding))
    client = SessionClient(runtime, SessionConfig(robot_id="arm-1", revision_id="rev_local"))
    connection = LocalConnection(client, InProcessPolicy(policy))
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

        failing = True  # every request is refused now: the queue runs dry and the robot stops
        await until(lambda: runtime.state is RuntimeState.FAULTED)
        assert connection.counters[ReasonCode.WORKER_INFERENCE_FAILED.value] > 0
        commands = len(controller.commands)
        await asyncio.sleep(0.3)
        assert len(controller.commands) == commands
    finally:
        stop.set()
        await serving
    assert monitor.violations == 0, monitor.summary()
