"""The runtime without a gateway, through ``LocalConnection``: an ``@neura.policy`` class in this
process and over the network (``neura-robot serve``), and policies behind openpi's own server and
LeRobot's async inference server.

The openpi case needs Docker and its image: build it with
``docker build -t neura-openpi-test:dev packages/neura-runtime/tests/openpi`` and set
``NEURA_TEST_OPENPI_IMAGE=neura-openpi-test:dev``; it is skipped otherwise. The LeRobot case needs
LeRobot (the lerobot extra, Python 3.12+).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import pickle
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from neura.v1 import worker_pb2
from neura_contracts import ReasonCode
from neura_runtime import remote, replay
from neura_runtime.authored import AuthoredPolicy
from neura_runtime.binding import ExecutionBinding, load_binding
from neura_runtime.incident import IncidentRecorder
from neura_runtime.invariants import InvariantMonitor, watch_robot
from neura_runtime.lerobot_server import LeRobotPolicy, unpickle_actions
from neura_runtime.local import LocalConnection
from neura_runtime.openpi import OpenPIKeys, OpenPIPolicy, StateKey
from neura_runtime.policy import InProcessPolicy, PolicyClient, PolicyError
from neura_runtime.runtime import RobotRuntime, RuntimeState
from neura_runtime.session import SessionClient, SessionConfig
from neura_runtime.testing import FakeController, FakeSensors

EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "contracts"
OPENPI_IMAGE = os.environ.get("NEURA_TEST_OPENPI_IMAGE", "")


def openpi_image_present() -> bool:
    if not OPENPI_IMAGE or shutil.which("docker") is None:
        return False
    found = subprocess.run(["docker", "image", "inspect", OPENPI_IMAGE], capture_output=True)
    return found.returncode == 0  # no Docker daemon, or no image: the case is skipped


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
        assert time.monotonic() < deadline, condition
        await asyncio.sleep(0.01)


@asynccontextmanager
async def openpi_server(binding: ExecutionBinding, work: Path) -> AsyncIterator[OpenPIPolicy]:
    """openpi's own server in a container, serving tests/openpi/serve_still.py."""
    layout = binding.state_layouts[0]
    name = f"neura-openpi-{uuid.uuid4().hex[:8]}"
    sizes = [binding.chunk_length, binding.action_dim, len(layout.feature_names)]
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::8000",
         "-v", f"{work}:/work:ro", "-v", f"{Path(__file__).parent / 'openpi'}:/opt/test:ro",
         OPENPI_IMAGE, "python", "/opt/test/serve_still.py", *map(str, sizes)],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    try:
        port = (
            subprocess.run(
                ["docker", "port", name, "8000"], check=True, capture_output=True, text=True
            )
            .stdout.split(":")[-1]
            .strip()
        )
        keys = OpenPIKeys(
            state=[StateKey("observation/state", layout.input_name)],
            images={"observation/image": "front", "observation/wrist_image": "wrist"},
        )
        client = OpenPIPolicy(f"ws://127.0.0.1:{port}", keys)
        deadline = time.monotonic() + 30
        while True:  # the server takes a moment to listen
            try:
                assert (await client.connect()) == {"policy": "still"}
                break
            except PolicyError:
                assert time.monotonic() < deadline, "openpi's server did not start"
                await asyncio.sleep(0.2)
        yield client
        await client.close()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


@asynccontextmanager
async def lerobot_server(binding: ExecutionBinding, work: Path) -> AsyncIterator[Reached]:
    """LeRobot's own policy server in a process of its own, serving a synthetic ACT checkpoint
    made for these contracts."""
    from neura_runtime.synthetic import write_act_checkpoint

    checkpoint = write_act_checkpoint(binding.artifact, work / "act")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    serve = (
        "from lerobot.async_inference.configs import PolicyServerConfig\n"
        "from lerobot.async_inference.policy_server import serve\n"
        f"serve(PolicyServerConfig(host='127.0.0.1', port={port}, fps=30, inference_latency=0.0))"
    )
    # LeRobot leaves a file in TMPDIR for every policy it loads: give it one under the test's.
    scratch = work / "lerobot-tmp"
    scratch.mkdir()
    with (work / "lerobot.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-c", serve],
            stdout=log,
            stderr=log,
            cwd=work,
            env={**os.environ, "TMPDIR": str(scratch)},
        )
    client = LeRobotPolicy(f"127.0.0.1:{port}", binding, policy_type="act", checkpoint=checkpoint)
    try:
        deadline = time.monotonic() + 120
        while True:  # the server takes a while to import LeRobot and listen
            try:
                await client.load()
                break
            except PolicyError:
                assert process.poll() is None, (work / "lerobot.log").read_text()[-2000:]
                assert time.monotonic() < deadline, "LeRobot's server did not start"
                await asyncio.sleep(0.5)
        yield Reached(client, process.kill, ReasonCode.WORKER_UNAVAILABLE)
        await client.close()
    finally:
        process.kill()
        process.wait()


@dataclass
class Reached:
    policy: PolicyClient
    fail: Callable[[], None]  # from now on, every request fails
    reason: ReasonCode  # what the robot is told when one does


@asynccontextmanager
async def reached(
    how: str, policy: AuthoredPolicy, binding: ExecutionBinding, work: Path
) -> AsyncIterator[Reached]:
    def fail() -> None:
        (work / "fail").touch()  # still.py and serve_still.py fail from now on

    failed = ReasonCode.WORKER_INFERENCE_FAILED
    if how == "in-process":
        yield Reached(InProcessPolicy(policy), fail, failed)
        return
    if how == "openpi":
        async with openpi_server(binding, work) as openpi:
            yield Reached(openpi, fail, failed)
        return
    if how == "lerobot":
        async with lerobot_server(binding, work) as served:
            yield served
        return
    description = worker_pb2.WorkerDescription(chunk_length=policy.declaration.chunk_length)
    server, port = await remote.serve(
        policy, "127.0.0.1:0", description=description, token="secret"
    )
    client = remote.RemotePolicy(f"127.0.0.1:{port}", token="secret")
    try:
        await client.check(binding)
        yield Reached(client, fail, failed)
    finally:
        await client.close()
        await server.stop(grace=None)


LEROBOT = importlib.util.find_spec("lerobot") is not None


@pytest.mark.parametrize(
    "how",
    [
        "in-process",
        "served",
        pytest.param(
            "openpi",
            marks=pytest.mark.skipif(
                not openpi_image_present(),
                reason="needs Docker and NEURA_TEST_OPENPI_IMAGE (tests/openpi/Dockerfile)",
            ),
        ),
        pytest.param(
            "lerobot",
            marks=pytest.mark.skipif(
                not LEROBOT, reason="needs LeRobot (the lerobot or so101 extra, Python 3.12+)"
            ),
        ),
    ],
)
async def test_a_policy_drives_the_runtime_and_its_failures_stop_it(
    how: str, tmp_path: Path
) -> None:
    profile = EXAMPLES / "act-so101-30hz.profile.yaml"
    if how == "lerobot":
        # ACT behind LeRobot's server takes 0.3 to 0.5 s a chunk on this CPU, too slow for the
        # 30 Hz profile's 150 ms: the robot rightly refuses every chunk. Declare what it allows.
        document = yaml.safe_load(profile.read_text())
        document["spec"]["freshness"] = {
            "max_observation_age_at_dispatch_us": 1_000_000,
            "max_observation_age_at_acceptance_us": 1_500_000,
            "request_timeout_us": 2_000_000,
        }
        profile = tmp_path / "act-so101-cpu.profile.yaml"
        profile.write_text(yaml.safe_dump(document))
    binding = load_binding(
        EXAMPLES / "act-so101.artifact.yaml", EXAMPLES / "so101.robot.yaml", profile
    )
    source = tmp_path / "still.py"
    source.write_text(
        POLICY.format(chunk_length=binding.chunk_length, action_dim=binding.action_dim)
    )
    controller = FakeController()
    recorder = IncidentRecorder(tmp_path / "incidents", robot_id="arm-1")
    runtime = RobotRuntime(
        binding, recorder.controller(controller), recorder.sensors(FakeSensors(binding))
    )
    recorder.attach(runtime)
    client = SessionClient(runtime, SessionConfig(robot_id="arm-1", revision_id="rev_local"))
    async with reached(how, AuthoredPolicy.load(str(source)), binding, tmp_path) as served:
        connection = LocalConnection(client, served.policy)
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

            served.fail()  # every request fails now: the robot runs dry and stops
            await until(lambda: runtime.state is RuntimeState.FAULTED)
            assert connection.counters[served.reason.value] > 0
            commands = len(controller.commands)
            await asyncio.sleep(0.3)
            assert len(controller.commands) == commands
        finally:
            stop.set()
            await serving
    assert monitor.violations == 0, monitor.summary()

    # The fault wrote its incident record, and a fresh runtime given it decides the same.
    recorder.close()
    assert len(recorder.written) == 1
    record = replay.read(recorder.written[0])
    assert any(e["kind"] == "fault" for e in record.events)
    outcome = replay.replay(record)
    assert outcome.matches, (outcome.first_difference, outcome.recorded, outcome.replayed)
    # A record that says one command failed where it succeeded no longer replays the same.
    failed = next(e for e in record.events if e["kind"] == "command_result")
    failed["ok"] = False
    assert not replay.replay(record).matches


@pytest.mark.skipif(not LEROBOT, reason="needs LeRobot (the lerobot or so101 extra, Python 3.12+)")
def test_a_lerobot_reply_is_unpickled_only_as_actions() -> None:
    import torch
    from lerobot.async_inference.helpers import TimedAction

    actions = [TimedAction(timestamp=1.0, timestep=7, action=torch.ones(6))]
    assert unpickle_actions(pickle.dumps(actions))[0].action.tolist() == [1.0] * 6

    class Hostile:
        def __reduce__(self) -> tuple[object, tuple[str]]:
            return (os.system, ("touch /tmp/owned",))

    with pytest.raises(pickle.UnpicklingError, match="may not contain"):
        unpickle_actions(pickle.dumps([Hostile()]))
