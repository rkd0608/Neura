"""A Universal Robots arm through its own stack: URSim (Universal Robots' simulator, running the
real UR controller software) in one container, Universal Robots' official ROS 2 driver
(ur_robot_driver, its forward position controller) and the relay node in another, and
``neura-arm``'s process on this machine driving the arm through them, until the driver stops.

What it cannot test: the link between the driver and the controller. When it is cut, the driver
goes on publishing the last joint states at 500 Hz and reports its program running for 20 s and
more, so nothing on the ROS side shows the robot is gone (the controller's own watchdog stops a
real arm); see docs/ros2.md.

Needs Docker, URSim's image and the driver's: build the second with
``docker build -t neura-ur-driver-test:dev packages/neura-robots/tests/ursim`` and set
``NEURA_TEST_UR_DRIVER_IMAGE=neura-ur-driver-test:dev`` (and ``NEURA_TEST_URSIM_IMAGE`` to use
a URSim image other than ``universalrobots/ursim_e-series``). Skipped otherwise. URSim takes
about a minute to boot.
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
from typing import Any

import pytest

from neura_robots.policies import PolicyConfig, inferred_binding, open_policy
from neura_robots.process import ArmProcess
from neura_robots.ros2 import relay
from neura_robots.ros2.arm import Ros2Arm, Ros2Config, Ros2Joint
from neura_runtime.invariants import InvariantMonitor
from neura_runtime.runtime import RuntimeState

DRIVER = os.environ.get("NEURA_TEST_UR_DRIVER_IMAGE", "")
URSIM = os.environ.get(
    "NEURA_TEST_URSIM_IMAGE",
    "universalrobots/ursim_e-series@sha256:c80dfb4ec6b554db6d20ba45fb90d0ec056d0303bd3c7a7040a4e8b503eaaf87",
)
# The forward position controller's joints, in its command order.
JOINTS = [
    "shoulder_pan_joint",
    "shoulder_lift_joint",
    "elbow_joint",
    "wrist_1_joint",
    "wrist_2_joint",
    "wrist_3_joint",
]


def docker(*args: str, timeout: float = 120) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    assert done.returncode == 0, f"docker {' '.join(args[:3])}: {done.stderr.strip()}"
    return done.stdout.strip()


def images_present() -> bool:
    if not DRIVER or shutil.which("docker") is None:
        return False
    done = subprocess.run(
        ["docker", "image", "inspect", DRIVER], capture_output=True, text=True, timeout=60
    )
    return done.returncode == 0  # no Docker daemon, or no image: the test is skipped


pytestmark = pytest.mark.skipif(
    not images_present(), reason="needs Docker and NEURA_TEST_UR_DRIVER_IMAGE (tests/ursim)"
)

# The UR5e's joint ranges: two turns either way, the elbow half that.
ROBOT: dict[str, Any] = {
    "api_version": "neura.dev/v1alpha1",
    "kind": "RobotProfile",
    "metadata": {"name": "ur5e-ros2", "version": "r1"},
    "spec": {
        "controller": {"name": "ur-robot-driver-forward-position", "revision": "jazzy"},
        "control_period_us": 20_000,
        "command_interface": [
            {
                "name": joint,
                "unit": "rad",
                "frame": "joint_space",
                "semantics": "absolute_joint_position",
                "min": -3.14159 if joint == "elbow_joint" else -6.28318,
                "max": 3.14159 if joint == "elbow_joint" else 6.28318,
            }
            for joint in JOINTS
        ],
        "state_features": [{"name": j, "unit": "rad", "frame": "joint_space"} for j in JOINTS],
        "cameras": [],
        "fault_responses": ["hold_position", "controlled_stop"],
    },
}

# Every joint toward its target from where the arm is, at most 0.25 rad/s: a real controller
# refuses a jump (a protective stop), so the policy moves the way a trained one would.
TOWARD = """
import numpy as np
import neura_contracts.authoring as neura

TARGET = np.array({target}, dtype=np.float32)
STEP = np.float32(0.005)  # rad per 20 ms


@neura.policy(chunk_length=25, cameras={{}})
class Toward:
    def act(self, observation):
        state = np.asarray(observation["observation.state"], dtype=np.float32)
        reach = STEP * np.arange(1, 26, dtype=np.float32)[:, None]
        return state + np.clip(TARGET - state, -reach, reach)
"""

DASHBOARD = """
import socket, sys, time
for attempt in range(120):
    try:
        s = socket.create_connection(("ursim", 29999), timeout=5)
        s.recv(200)
        break
    except OSError:
        time.sleep(1)
for command in ("power on", "brake release"):
    s.sendall(command.encode() + b"\\n")
    print(s.recv(200).decode().strip(), flush=True)
    time.sleep(2)
for attempt in range(60):
    s.sendall(b"robotmode\\n")
    if "RUNNING" in s.recv(200).decode():
        sys.exit(0)
    time.sleep(1)
sys.exit("URSim never reached RUNNING")
"""


@contextmanager
def ur_robot(sockets: Path) -> Iterator[Callable[[], None]]:
    """URSim and the driver with the relay, on their own network; the relay's socket is in
    ``sockets``. Yields what crashes the driver (its controller manager, which publishes the
    joint states and takes the commands)."""
    name = f"neura-ursim-{uuid.uuid4().hex[:8]}"
    docker("network", "create", name)
    try:
        docker(
            "run", "-d", "--rm", "--network", name, "--network-alias", "ursim",
            "--name", f"{name}-ursim", "-e", "ROBOT_MODEL=UR5", URSIM, timeout=600,
        )  # fmt: skip
        docker(
            "run", "-d", "--rm", "--network", name, "--name", f"{name}-driver",
            "-e", "HOME=/tmp", "-v", f"{Path(relay.__file__).parent}:/opt/relay:ro",
            "-v", f"{sockets}:/run/neura", DRIVER, "bash", "-c",
            "source /opt/ros/jazzy/setup.bash"
            f" && python3 -c '{DASHBOARD}'"
            " && (ros2 launch ur_robot_driver ur_control.launch.py ur_type:=ur5e"
            " robot_ip:=ursim launch_rviz:=false headless_mode:=true"
            " initial_joint_controller:=forward_position_controller &)"
            f" && exec python3 /opt/relay/relay.py --socket /run/neura/ros2.sock"
            f" --joints {','.join(JOINTS)}",
        )  # fmt: skip

        def stop_driver() -> None:
            docker("exec", f"{name}-driver", "pkill", "-KILL", "-f", "ros2_control_node")

        yield stop_driver
    finally:
        subprocess.run(
            ["docker", "rm", "-f", f"{name}-ursim", f"{name}-driver"],
            capture_output=True,
            timeout=120,
        )
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=60)


async def until(condition: Callable[[], bool], said: list[str], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "\n".join(said[-8:])
        await asyncio.sleep(0.02)


async def test_a_ur_arm_is_driven_through_its_own_controller_and_driver(tmp_path: Path) -> None:
    socket = tmp_path / "ros2.sock"
    tmp_path.chmod(0o777)  # the relay runs as root in its container
    said: list[str] = []
    with ur_robot(tmp_path) as stop_driver:
        await until(socket.exists, said, timeout=240)  # URSim boots, then the driver
        joints = [Ros2Joint(j, j, 1.0, 0.0) for j in JOINTS]
        config = Ros2Config(socket, joints, [], 100.0, 120.0)
        probe = Ros2Arm(config, inferred_binding_for([0.0] * 6, tmp_path).robot)
        await asyncio.to_thread(probe.connect)  # the driver's joint states are flowing
        start = probe.sample()
        assert start is not None
        probe.close()

        target = [start[j] + (0.4 if j in ("shoulder_pan_joint", "wrist_3_joint") else 0.0)
                  for j in JOINTS]  # fmt: skip
        binding = inferred_binding_for(target, tmp_path)
        policy = await open_policy(PolicyConfig(uri="toward.py"), binding, tmp_path)
        arm = Ros2Arm(config, binding.robot)
        await asyncio.to_thread(arm.connect)
        process = ArmProcess(arm, binding, "ur5e", "", "", "", say=said.append, policy=policy)
        monitor = InvariantMonitor(
            clock=lambda: time.monotonic_ns() // 1000, gateway_observed=False
        )
        process.watch(monitor)
        runtime, sent = process.runtime, arm.counters
        stop = asyncio.Event()
        serving = asyncio.create_task(process.connection.run(stop))
        try:
            await until(lambda: runtime.state is RuntimeState.READY, said)
            await asyncio.sleep(1.0)
            assert sent["commands"] == 0  # never arms on its own
            still = arm.sample()
            assert still is not None
            assert abs(still["shoulder_pan_joint"] - start["shoulder_pan_joint"]) < 0.01

            process.inbox.submit("arm")
            await until(lambda: runtime.dispatched > 150, said, timeout=15)  # three seconds
            moved = arm.sample()
            assert moved is not None
            # The controller followed: about 0.25 rad/s for three seconds, toward the target.
            assert moved["shoulder_pan_joint"] - start["shoulder_pan_joint"] > 0.2, (start, moved)

            process.inbox.submit("takeover")
            await until(lambda: runtime.state is RuntimeState.READY, said)
            held = arm.sample()
            assert held is not None
            count = sent["commands"]
            await asyncio.sleep(1.0)
            assert sent["commands"] == count
            after = arm.sample()
            assert after is not None
            assert abs(after["shoulder_pan_joint"] - held["shoulder_pan_joint"]) < 0.02

            process.inbox.submit("arm")  # a new task, from where the arm holds
            await until(lambda: runtime.state is RuntimeState.RUNNING, said)
            stop_driver()  # the driver crashes: no joint states, and the runtime stops
            await until(lambda: runtime.state is RuntimeState.FAULTED, said, timeout=3.0)
            count = sent["commands"]
            await asyncio.sleep(0.5)
            assert sent["commands"] == count
        finally:
            stop.set()
            await serving
            arm.close()
    assert monitor.violations == 0, monitor.summary()


def inferred_binding_for(target: list[float], directory: Path) -> Any:
    """The contracts for the toward-target policy on the UR5e, inferred as for any policy."""
    (directory / "toward.py").write_text(TOWARD.format(target=target))
    return inferred_binding(PolicyConfig(uri="toward.py"), ROBOT, directory)
