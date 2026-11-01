"""A recorded SO-101 (``neura_robots.recorded``) behind ``neura-arm``'s robot process: an episode
of a real arm's dataset on the Hugging Face Hub (lerobot/svla_so101_pickplace, its two cameras
as the profile's front and wrist) served to the runtime and a policy at the speed it was
recorded, until the recording ends.

Needs the lerobot extra, and the dataset: fetched from the Hub once (86 MB), then cached.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("lerobot.datasets.lerobot_dataset", exc_type=ImportError)

from neura_robots.policies import PolicyConfig, open_policy
from neura_robots.process import ArmProcess
from neura_robots.recorded import RecordedArm, RecordedEpisode, describe
from neura_runtime.binding import load_binding
from neura_runtime.invariants import InvariantMonitor
from neura_runtime.runtime import RuntimeState

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "examples" / "contracts"
# The example policy: at the root of the published repository, under open-source/ in Neura's.
POLICY = next(
    p / "policies" / "mid_range.py"
    for p in (ROOT / "examples", ROOT / "open-source" / "examples")
    if (p / "policies" / "mid_range.py").is_file()
)
DATASET = "lerobot/svla_so101_pickplace"


async def until(condition: Callable[[], bool], said: list[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "\n".join(said[-8:])
        await asyncio.sleep(0.02)


async def test_a_recorded_arm_drives_the_runtime_until_the_recording_ends() -> None:
    binding = load_binding(
        CONTRACTS / "act-so101.artifact.yaml",
        CONTRACTS / "so101.robot.yaml",
        CONTRACTS / "act-so101-30hz.profile.yaml",
    )
    episode = await asyncio.to_thread(RecordedEpisode, DATASET, 0)
    assert episode.fps == 30
    assert episode.length > 150
    arm = RecordedArm(episode, binding.robot, {"front": "up", "wrist": "side"})
    policy = await open_policy(PolicyConfig(uri=str(POLICY)), binding, Path("."))
    await asyncio.to_thread(arm.connect)
    said: list[str] = []
    process = ArmProcess(arm, binding, "so101-recorded", "", "", "", say=said.append, policy=policy)
    monitor = InvariantMonitor(clock=lambda: time.monotonic_ns() // 1000, gateway_observed=False)
    process.watch(monitor)
    runtime = process.runtime
    stop = asyncio.Event()
    serving = asyncio.create_task(process.connection.run(stop))
    try:
        await until(lambda: runtime.state is RuntimeState.READY, said)
        start = arm.sample()
        process.inbox.submit("arm")
        await until(lambda: runtime.dispatched > 60, said)  # two seconds at 30 Hz
        moved = arm.sample()
        assert start is not None
        assert moved is not None
        assert max(abs(moved[k] - start[k]) for k in start) > 1.0  # the recorded arm moves
        report = describe(arm)
        assert report["commands_compared"] > 60
        # The policy holds mid-range while the recorded arm picks: far from what was recorded.
        assert max(report["mean_abs_error"].values()) > 1.0

        # The recording ends: the robot is gone, and the runtime stops.
        await until(lambda: runtime.state is RuntimeState.FAULTED, said, timeout=episode.length)
        count = arm.counters["commands"]
        await asyncio.sleep(0.3)
        assert arm.counters["commands"] == count
    finally:
        stop.set()
        await serving
        arm.close()
    assert monitor.violations == 0, monitor.summary()
