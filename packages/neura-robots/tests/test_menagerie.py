"""The MuJoCo Menagerie arms (``neura_robots.sim.menagerie``) behind ``neura-arm``'s robot process:
for each arm, contracts inferred from its model and a policy file, the runtime driving it in real
time with its camera on its own thread, the operator's authority, and the invariant monitor.

Needs the sim extra and the Menagerie models: fetched once with git, or the checkout
``NEURA_MENAGERIE`` names.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("neura_robots.sim")  # MuJoCo's OpenGL backend, or what is missing
pytest.importorskip("mujoco")

from neura_robots.cli import MujocoArmConfig, RobotConfig
from neura_robots.policies import open_policy
from neura_robots.process import ArmProcess
from neura_robots.sim.arm import SimArm
from neura_robots.sim.menagerie import MODELS
from neura_runtime.invariants import InvariantMonitor
from neura_runtime.runtime import RuntimeState

ROOT = Path(__file__).resolve().parents[3]
# The example: at the root of the published repository, under open-source/ in Neura's.
EXAMPLES = next(
    p
    for p in (ROOT / "examples", ROOT / "open-source" / "examples")
    if (p / "robots" / "panda-sim.yaml").is_file()
)

# Every command toward the middle of its range: each arm starts at home, off the middle.
MID_RANGE = """
import numpy as np
import neura_contracts.authoring as neura


@neura.policy(chunk_length=25, cameras={{}})
class MidRange:
    def act(self, observation):
        return np.tile(np.array({targets}, dtype=np.float32), (25, 1))
"""


async def until(condition: Callable[[], bool], said: list[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, said
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("model", list(MODELS))
async def test_a_menagerie_arm_is_driven_with_inferred_contracts(
    model: str, tmp_path: Path
) -> None:
    from neura_robots.cli import _scene

    commands = _scene(model).commands
    targets = [(c.low + c.high) / 2 for c in commands]
    (tmp_path / "mid_range.py").write_text(MID_RANGE.format(targets=targets))
    config = RobotConfig.model_validate(
        {
            "robot_id": f"{model}-sim",
            "policy": {"uri": "mid_range.py"},
            "arm": {"type": "mujoco", "model": model},
        }
    )
    binding = config.binding(tmp_path)
    assert binding is not None
    assert [c.name for c in binding.robot.spec.command_interface] == [c.name for c in commands]
    policy = await open_policy(config.policy, binding, tmp_path)  # type: ignore[arg-type]
    arm = config.open_arm(binding.robot)
    assert isinstance(arm, SimArm)
    arm.connect()
    said: list[str] = []
    process = ArmProcess(arm, binding, config.robot_id, "", "", "", say=said.append, policy=policy)
    monitor = InvariantMonitor(clock=lambda: time.monotonic_ns() // 1000, gateway_observed=False)
    process.watch(monitor)
    runtime, sent = process.runtime, arm.counters
    stop = asyncio.Event()
    serving = asyncio.create_task(process.connection.run(stop))
    try:
        await until(lambda: runtime.state is RuntimeState.READY, said)
        start = arm.sample()
        await asyncio.sleep(0.5)
        assert sent["commands"] == 0  # never arms on its own

        process.inbox.submit("arm")
        await until(lambda: runtime.dispatched > 100, said)  # two seconds at 50 Hz
        moved = arm.sample()

        def off(at: dict[str, float]) -> float:  # how far from the targets, in ranges
            return sum(
                ((at[c.name] - t) / (c.high - c.low)) ** 2
                for c, t in zip(commands, targets, strict=True)
            )

        assert off(moved) < 0.25 * off(start), (start, moved)  # at least halfway there

        process.inbox.submit("takeover")
        await until(lambda: runtime.state is RuntimeState.READY, said)
        count = sent["commands"]
        await asyncio.sleep(0.3)
        assert sent["commands"] == count
    finally:
        stop.set()
        await serving
        assert arm.close()  # holds where it is
    assert monitor.violations == 0, monitor.summary()


def test_the_simulated_panda_example_fits_its_arm() -> None:
    config = RobotConfig.load(EXAMPLES / "robots" / "panda-sim.yaml")
    assert isinstance(config.arm, MujocoArmConfig)
    binding = config.binding(EXAMPLES / "robots")
    assert binding is not None
    assert binding.chunk_length == 25
