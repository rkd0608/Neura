"""``neura-arm`` end to end: the emulated arm through LeRobot's bus driver, driven by operator
commands and served by a policy in this process, or by a live site gateway over gRPC (the gateway
is not part of neura-robot: those cases are skipped without it)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("lerobot", reason="needs LeRobot (the lerobot or so101 extra, Python 3.12+)")

from neura_contracts import ReasonCode, RobotProfile
from neura_robots.emulator import SO101Emulator, write_calibration
from neura_robots.policies import PolicyConfig, open_policy
from neura_robots.process import ArmProcess, AssignedArm, OperatorInbox
from neura_robots.so101 import CameraConfig, So101Arm, So101Config
from neura_runtime.binding import load_binding
from neura_runtime.invariants import InvariantMonitor
from neura_runtime.runtime import RuntimeState

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"
FILES = (
    EXAMPLES / "act-so101.artifact.yaml",
    EXAMPLES / "so101.robot.yaml",
    EXAMPLES / "act-so101-30hz.profile.yaml",
)
CAMERAS = {"front": CameraConfig("synthetic"), "wrist": CameraConfig("synthetic")}


@asynccontextmanager
async def site_gateway() -> AsyncIterator[str]:
    """A site gateway (Neura's site layer, not part of neura-robot) serving a mock policy."""
    site = pytest.importorskip("neura_server.config", reason="needs Neura's site gateway")
    grpc_server = pytest.importorskip("neura_server.grpc_server")
    artifact, robot, profile = FILES
    server = grpc_server.GatewayServer(
        site.GatewayConfig(
            listen="127.0.0.1:0",
            status_listen=None,
            devices=[
                site.DeviceConfig(robot_id="arm-1", token_sha256=site.token_digest("token-arm-1"))
            ],
            deployments=[
                site.DeploymentConfig(
                    revision_id="rev_arm",
                    artifact=artifact,
                    robot_profile=robot,
                    execution_profile=profile,
                    robots=["arm-1"],
                )
            ],
            assignments={"arm-1": "rev_arm"},
            serving=site.ServingConfig(backend="simulated", simulate_latency=True),
        )
    )
    await server.start()
    try:
        yield f"127.0.0.1:{server.bound_port}"
    finally:
        await server.stop()


@pytest.fixture
async def gateway() -> AsyncIterator[str]:
    async with site_gateway() as target:
        yield target


@pytest.fixture
def emulator() -> Iterator[SO101Emulator]:
    # Off-centre: the gateway's mock policy moves every joint toward mid-range.
    with SO101Emulator(positions_deg={"shoulder_pan": 20.0, "elbow_flex": -15.0}) as e:
        yield e


@pytest.fixture
def config(emulator: SO101Emulator, tmp_path: Path) -> So101Config:
    write_calibration(emulator, tmp_path / "calibration", "arm-1")
    return So101Config(
        port=emulator.port,
        calibration_id="arm-1",
        calibration_dir=tmp_path / "calibration",
        cameras=CAMERAS,
        lock_dir=tmp_path / "locks",
    )


class Served:
    def __init__(self, process: ArmProcess, said: list[str]) -> None:
        self.process = process
        self.said = said
        self.stop = asyncio.Event()
        self.task = asyncio.create_task(process.connection.run(self.stop))

    async def until(self, condition: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, (self.process.status(), self.said)
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        self.stop.set()
        await self.task


# Without a gateway: every joint toward mid-range, as the gateway's mock policy moves them.
MID_RANGE = """
import numpy as np
import neura_contracts.authoring as neura


@neura.policy(chunk_length={chunk_length})
class MidRange:
    def act(self, observation):
        return np.tile(np.array({targets}, dtype=np.float32), ({chunk_length}, 1))
"""


@pytest.fixture
async def served(
    config: So101Config, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[tuple[Served, list[str]]]:
    binding = load_binding(*FILES)
    policy = None
    gateway = ""
    stack = AsyncExitStack()
    if getattr(request, "param", "policy") == "gateway":
        gateway = await stack.enter_async_context(site_gateway())
    else:
        targets = [(c.min + c.max) / 2 for c in binding.robot.spec.command_interface]
        source = tmp_path / "mid_range.py"
        source.write_text(MID_RANGE.format(chunk_length=binding.chunk_length, targets=targets))
        policy = await open_policy(PolicyConfig(uri=source.name), binding, tmp_path)
    arm = So101Arm(config, binding.robot)
    arm.connect()
    said: list[str] = []
    process = ArmProcess(
        arm, binding, "arm-1", "rev_arm", gateway, "token-arm-1", say=said.append, policy=policy
    )
    monitor = InvariantMonitor(clock=lambda: time.monotonic_ns() // 1000, gateway_observed=False)
    process.watch(monitor)
    served = Served(process, said)
    yield served, said
    await served.close()
    await stack.aclose()
    arm.close()
    assert monitor.violations == 0, monitor.summary()


@pytest.mark.parametrize("served", ["policy", "gateway"], indirect=True)
async def test_nothing_moves_until_the_operator_arms(
    served: tuple[Served, list[str]], emulator: SO101Emulator
) -> None:
    robot, said = served
    runtime = robot.process.runtime
    await robot.until(lambda: runtime.state is RuntimeState.READY)
    start = emulator.positions()
    await asyncio.sleep(1.0)
    assert runtime.state is RuntimeState.READY  # never arms on its own
    assert robot.process.arm.counters["commands"] == 0
    assert emulator.positions() == pytest.approx(start, abs=0.2)
    assert any("type arm" in line for line in said)

    robot.process.inbox.submit("arm")
    await robot.until(lambda: runtime.state is RuntimeState.RUNNING and runtime.dispatched > 30)
    assert robot.process.arm.counters["commands"] >= 30
    moved = emulator.positions()
    assert max(abs(moved[k] - start[k]) for k in start) > 0.5

    robot.process.inbox.submit("takeover")
    await robot.until(lambda: runtime.state is RuntimeState.READY)
    commands = robot.process.arm.counters["commands"]
    await asyncio.sleep(0.5)
    assert robot.process.arm.counters["commands"] == commands
    assert any(line == "taken over; holding" for line in said)

    robot.process.inbox.submit("status")
    await robot.until(lambda: any(line.startswith("{") for line in said))
    status = json.loads(next(line for line in said if line.startswith("{")))
    assert status["state"] == "Ready"
    arm = robot.process.arm
    assert isinstance(arm, So101Arm)
    assert set(status["positions"]) == set(arm.commands)


async def test_a_bus_failure_latches_and_reset_needs_a_working_arm(
    served: tuple[Served, list[str]], emulator: SO101Emulator
) -> None:
    robot, said = served
    runtime = robot.process.runtime
    await robot.until(lambda: runtime.state is RuntimeState.READY)
    robot.process.inbox.submit("arm")
    await robot.until(lambda: runtime.state is RuntimeState.RUNNING)

    emulator.set_fault(silent=True)  # the USB adapter is pulled
    await robot.until(lambda: runtime.state is RuntimeState.FAULTED)
    fault = runtime.fault
    assert fault is not None
    assert fault.reason is ReasonCode.CONTROLLER_COMMAND_FAILED
    assert not fault.response_confirmed  # nobody can confirm a hold on a dead bus
    assert any("NOT CONFIRMED" in line for line in said)

    robot.process.inbox.submit("reset")
    await robot.until(lambda: any(line.startswith("reset refused") for line in said))
    assert runtime.state is RuntimeState.FAULTED

    emulator.set_fault(silent=False)
    robot.process.inbox.submit("reset")
    await robot.until(lambda: runtime.state is RuntimeState.READY)
    assert "reset: ready" in said


async def test_a_joint_that_loses_torque_faults_the_robot(
    served: tuple[Served, list[str]], emulator: SO101Emulator
) -> None:
    robot, _ = served
    runtime = robot.process.runtime
    await robot.until(lambda: runtime.state is RuntimeState.READY)
    robot.process.inbox.submit("arm")
    await robot.until(lambda: runtime.state is RuntimeState.RUNNING)
    emulator.set_fault("elbow_flex", overload=True)
    await robot.until(lambda: runtime.state is RuntimeState.FAULTED)
    assert runtime.fault is not None
    assert "elbow_flex" in (runtime.fault.detail or "")

    robot.process.inbox.submit("reset")  # recover() re-enables torque without a jump
    await robot.until(lambda: runtime.state is RuntimeState.READY)
    assert all(emulator.torque_enabled().values())


async def test_stale_operator_commands_are_dropped(served: tuple[Served, list[str]]) -> None:
    robot, said = served
    runtime = robot.process.runtime
    robot.process.inbox.max_age_s = 0.0
    robot.process.inbox.submit("arm")
    await robot.until(lambda: any(line.startswith("ignored 'arm'") for line in said))
    await asyncio.sleep(0.5)
    assert runtime.state is not RuntimeState.ARMED
    assert runtime.dispatched == 0


async def test_the_assigned_revision_is_served(config: So101Config, gateway: str) -> None:
    opened: list[So101Arm] = []

    def open_arm(profile: RobotProfile) -> So101Arm:
        arm = So101Arm(config, profile)
        arm.connect()
        opened.append(arm)
        return arm

    said: list[str] = []
    inbox = OperatorInbox()
    assigned = AssignedArm(
        open_arm,
        "arm-1",
        gateway=gateway,
        credentials="token-arm-1",
        inbox=inbox,
        say=said.append,
        poll_seconds=0.2,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(assigned.run(stop))
    try:
        deadline = time.monotonic() + 10
        while assigned.process is None or assigned.process.runtime.state is not RuntimeState.READY:
            assert time.monotonic() < deadline, said
            await asyncio.sleep(0.05)
        assert assigned.process.revision_id == "rev_arm"
        inbox.submit("arm")
        while assigned.process.runtime.dispatched < 10:
            assert time.monotonic() < deadline + 5, said
            await asyncio.sleep(0.05)
        inbox.submit("quit")
        await asyncio.wait_for(task, timeout=10)
    finally:
        stop.set()
        await task
        for arm in opened:
            arm.close()
    assert "serving revision rev_arm" in said
    assert len(opened) == 1
