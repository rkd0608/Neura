"""The ROS 2 bridge end to end (design §29.8): a simulated ROS 2 arm in one ROS 2 Jazzy
container, its cameras and the relay node in another, and ``neura-arm``'s process on this machine
driving the arm through the relay with a policy in that process, while packets are dropped on
the arm's link and then the link is cut.

Needs Docker and the test image: build it with
``docker build -t neura-ros2-test:dev packages/neura-robots/tests/ros2`` and set
``NEURA_TEST_ROS2_IMAGE=neura-ros2-test:dev``. Skipped otherwise.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from neura_robots.cli import RobotConfig, Ros2ArmConfig
from neura_robots.policies import PolicyConfig, open_policy
from neura_robots.process import ArmProcess
from neura_robots.ros2 import relay
from neura_robots.ros2.arm import Ros2Arm
from neura_runtime.binding import load_binding
from neura_runtime.invariants import InvariantMonitor
from neura_runtime.runtime import ACTIVE_STATES, RuntimeState

ROOT = Path(__file__).resolve().parents[3]
FILES = tuple(
    ROOT / "examples" / "contracts" / name
    for name in ("act-so101.artifact.yaml", "so101.robot.yaml", "act-so101-30hz.profile.yaml")
)
EXAMPLE = ROOT / "examples" / "robots" / "ros2-arm.yaml"
IMAGE = os.environ.get("NEURA_TEST_ROS2_IMAGE", "")


def docker(*args: str, stdin: str | None = None) -> str:
    done = subprocess.run(
        ["docker", *args], input=stdin, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, f"docker {' '.join(args[:3])}: {done.stderr.strip()}"
    return done.stdout.strip()


def image_present() -> bool:
    if not IMAGE or shutil.which("docker") is None:
        return False
    done = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True, timeout=60
    )
    return done.returncode == 0  # no Docker daemon, or no image: the test is skipped


pytestmark = pytest.mark.skipif(
    not image_present(), reason="needs Docker and NEURA_TEST_ROS2_IMAGE (tests/ros2/Dockerfile)"
)


# Every joint toward mid-range: the arm starts off-centre, so it visibly moves.
MID_RANGE = """
import numpy as np
import neura_contracts.authoring as neura


@neura.policy(chunk_length={chunk_length})
class MidRange:
    def act(self, observation):
        return np.tile(np.array({targets}, dtype=np.float32), ({chunk_length}, 1))
"""


@contextmanager
def ros2_robot(sockets: Path) -> Iterator[Callable[[int], None]]:
    """The arm alone in one container; the cameras and the relay, whose socket is in
    ``sockets``, in another, on their own network. Yields what sets the percentage of packets
    the arm's container drops each way."""
    name = f"neura-ros2-{uuid.uuid4().hex[:8]}"
    joints = "shoulder_pan,shoulder_lift,elbow_flex,wrist_flex,wrist_roll,gripper"
    run = ["run", "-d", "--rm", "--network", name, "-e", "HOME=/tmp"]
    run += ["-v", f"{Path(__file__).parent / 'ros2'}:/opt/sim:ro"]
    run += ["-v", f"{Path(relay.__file__).parent}:/opt/relay:ro"]
    docker("network", "create", name)
    try:
        # Its DDS traffic over the network, where the drops are. Off-centre: the policy moves
        # every joint toward mid-range.
        docker(
            *run, "--name", f"{name}-arm", "--cap-add", "NET_ADMIN",
            "-e", "FASTDDS_BUILTIN_TRANSPORTS=UDPv4", IMAGE, "python3", "/opt/sim/sim_robot.py",
            "--joints", joints, "--start", "shoulder_pan=0.35,elbow_flex=-0.26,gripper=0.2",
        )  # fmt: skip
        docker(
            *run, "--name", f"{name}-relay", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{sockets}:/run/neura", IMAGE, "bash", "-c",
            "python3 /opt/sim/sim_robot.py --cameras front,wrist & exec python3"
            f" /opt/relay/relay.py --socket /run/neura/ros2.sock --joints {joints}"
            " --camera front=/camera/front/image_raw --camera wrist=/camera/wrist/image_raw",
        )  # fmt: skip

        def drop(percent: int) -> None:
            some = f"numgen random mod 100 lt {percent} " if percent < 100 else ""
            rule = f"meta l4proto udp {some}drop"
            docker(
                "exec", "-i", f"{name}-arm", "nft", "-f", "-",
                stdin=f"table inet loss\ndelete table inet loss\ntable inet loss {{\n"
                f"  chain in {{ type filter hook input priority 0; {rule}; }}\n"
                f"  chain out {{ type filter hook output priority 0; {rule}; }}\n}}\n",
            )  # fmt: skip

        yield drop
    finally:
        subprocess.run(
            ["docker", "rm", "-f", f"{name}-arm", f"{name}-relay"], capture_output=True, timeout=60
        )
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=60)


async def until(condition: Callable[[], bool], said: list[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, said
        await asyncio.sleep(0.02)


async def test_a_ros2_arm_is_driven_safely_over_a_lossy_link(tmp_path: Path) -> None:
    binding = load_binding(*FILES)
    targets = [(c.min + c.max) / 2 for c in binding.robot.spec.command_interface]
    (tmp_path / "mid_range.py").write_text(
        MID_RANGE.format(chunk_length=binding.chunk_length, targets=targets)
    )
    policy = await open_policy(PolicyConfig(uri="mid_range.py"), binding, tmp_path)
    example = RobotConfig.load(EXAMPLE)
    assert isinstance(example.arm, Ros2ArmConfig)
    socket = tmp_path / "ros2.sock"
    config = example.model_copy(
        update={"arm": example.arm.model_copy(update={"socket": socket, "connect_timeout_s": 30})}
    )
    said: list[str] = []
    with ros2_robot(tmp_path) as drop:
        await until(socket.exists, said, timeout=30)
        arm = config.open_arm(binding.robot)
        assert isinstance(arm, Ros2Arm)
        await asyncio.to_thread(arm.connect)  # DDS discovery between the containers first
        process = ArmProcess(
            arm, binding, "arm-1", "rev_arm", "", "", say=said.append, policy=policy
        )
        monitor = InvariantMonitor(
            clock=lambda: time.monotonic_ns() // 1000, gateway_observed=False
        )
        process.watch(monitor)
        runtime, sent = process.runtime, arm.counters
        stop = asyncio.Event()
        serving = asyncio.create_task(process.connection.run(stop))
        try:
            await until(lambda: runtime.state is RuntimeState.READY, said)
            start = arm.sample()
            assert start is not None
            await asyncio.sleep(1.0)
            assert sent["commands"] == 0  # never arms on its own

            process.inbox.submit("arm")
            await until(
                lambda: runtime.state is RuntimeState.RUNNING and runtime.dispatched > 30, said
            )
            moved = arm.sample()
            assert moved is not None
            assert max(abs(moved[k] - start[k]) for k in start) > 0.5

            drop(20)  # a fifth of the packets each way: the arm is still driven
            commands = sent["commands"]
            await asyncio.sleep(3.0)
            assert runtime.state in ACTIVE_STATES, said
            assert sent["commands"] - commands > 60

            drop(100)  # the link is cut: the joint states go stale and the runtime stops
            await until(lambda: runtime.state is RuntimeState.FAULTED, said, timeout=2.0)
            commands = sent["commands"]
            await asyncio.sleep(0.5)
            assert sent["commands"] == commands

            drop(0)  # back: driving again needs the operator's reset and arm
            await until(arm.has_authority, said)
            assert runtime.fault is not None  # latched
            process.inbox.submit("reset")
            await until(lambda: runtime.state is RuntimeState.READY, said)
            process.inbox.submit("arm")
            await until(lambda: runtime.state is RuntimeState.RUNNING, said)

            process.inbox.submit("takeover")
            await until(lambda: runtime.state is RuntimeState.READY, said)
            commands = sent["commands"]
            await asyncio.sleep(0.5)
            assert sent["commands"] == commands
        finally:
            stop.set()
            await serving
            arm.close()
    assert monitor.violations == 0, monitor.summary()
