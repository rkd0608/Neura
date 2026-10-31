"""Neura's robot runtime in a minute, with no robot, GPU or model download.

    python examples/quickstart.py

A simulated arm runs an @neura.policy through the same runtime a real robot uses. Nothing moves
until it is armed; the policy drives it; then the policy starts failing, the runtime faults and
holds, and the incident record it writes is replayed through a fresh runtime, which must decide
exactly the same.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from neura_runtime import replay
from neura_runtime.authored import AuthoredPolicy
from neura_runtime.binding import load_binding
from neura_runtime.incident import IncidentRecorder
from neura_runtime.local import LocalConnection
from neura_runtime.policy import InProcessPolicy, ModelInputs, PolicyError
from neura_runtime.runtime import RobotRuntime, RuntimeState
from neura_runtime.session import SessionClient, SessionConfig
from neura_runtime.testing import FakeController, FakeSensors

HERE = Path(__file__).resolve().parent
CONTRACTS = HERE / "contracts"


def say(text: str) -> None:
    print(f"  {text}", flush=True)


async def main() -> None:
    binding = load_binding(
        CONTRACTS / "act-so101.artifact.yaml",
        CONTRACTS / "so101.robot.yaml",
        CONTRACTS / "act-so101-30hz.profile.yaml",
    )
    authored = AuthoredPolicy.load(str(HERE / "policies" / "mid_range.py"))
    broken = False

    def policy(inputs: ModelInputs) -> NDArray[np.float32]:  # until we break it
        if broken:
            raise PolicyError("the model fell over")
        return authored(inputs)

    incidents = Path(tempfile.mkdtemp(prefix="neura-incidents-"))
    arm = FakeController()  # a simulated arm: it records what it is told to do
    recorder = IncidentRecorder(incidents, robot_id="demo-arm")
    runtime = RobotRuntime(
        binding, recorder.controller(arm), recorder.sensors(FakeSensors(binding))
    )
    recorder.attach(runtime)
    client = SessionClient(runtime, SessionConfig(robot_id="demo-arm", revision_id="quickstart"))
    connection = LocalConnection(client, InProcessPolicy(policy))
    stop = asyncio.Event()
    serving = asyncio.create_task(connection.run(stop))

    async def until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                raise SystemExit(f"timed out; the runtime is {runtime.state.value}")
            await asyncio.sleep(0.01)

    print("1. Connected. The runtime waits for an operator before anything moves.")
    await until(lambda: runtime.state is RuntimeState.READY)
    await asyncio.sleep(0.5)
    say(f"state {runtime.state.value}, commands sent to the arm: {len(arm.commands)}")

    print("2. Armed. The policy drives the arm at 30 Hz.")
    runtime.arm(time.monotonic_ns() // 1000)
    await until(lambda: runtime.dispatched >= 60)
    say(f"state {runtime.state.value}, commands sent: {len(arm.commands)}")
    say(f"last command: {[round(float(v), 1) for v in arm.commands[-1]]}")

    print("3. The policy starts failing. The arm runs out of fresh actions and the runtime stops.")
    broken = True
    await until(lambda: runtime.state is RuntimeState.FAULTED)
    sent = len(arm.commands)
    await asyncio.sleep(0.5)
    assert runtime.fault is not None
    say(f"fault: {runtime.fault.reason.value}; response: {runtime.fault.response}")
    say(f"commands after the fault: {len(arm.commands) - sent}")

    stop.set()
    await serving
    recorder.close()

    print("4. The fault wrote an incident record. Replaying it through a fresh runtime:")
    record_path = recorder.written[0]
    outcome = replay.replay(replay.read(record_path))
    say(f"{record_path}")
    say(f"{outcome.events} events; replays the same: {outcome.matches}")
    print("\nOpen the record in Foxglove (https://foxglove.dev) to see every decision in time.")


if __name__ == "__main__":
    asyncio.run(main())
