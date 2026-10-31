"""The runtime without a gateway, through ``LocalConnection``: an ``@neura.policy`` class in this
process and over the network (``neura-robot serve``), and a policy behind openpi's own server.

The openpi case needs Docker and its image: build it with
``docker build -t neura-openpi-test:dev packages/neura-runtime/tests/openpi`` and set
``NEURA_TEST_OPENPI_IMAGE=neura-openpi-test:dev``; it is skipped otherwise.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
import uuid
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
        assert time.monotonic() < deadline
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
async def reached(
    how: str, policy: AuthoredPolicy, binding: ExecutionBinding, work: Path
) -> AsyncIterator[PolicyClient]:
    if how == "in-process":
        yield InProcessPolicy(policy)
        return
    if how == "openpi":
        async with openpi_server(binding, work) as openpi:
            yield openpi
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
    ],
)
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
    async with reached(how, AuthoredPolicy.load(str(source)), binding, tmp_path) as policy:
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
