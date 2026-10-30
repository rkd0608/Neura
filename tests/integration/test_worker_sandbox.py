"""Policy workers in the container sandbox (design §12), with a real container runtime.

Needs Docker and a worker image: build it with
``docker build -f deploy/docker/Dockerfile --target neura-worker -t neura-worker:dev .`` and set
``NEURA_TEST_WORKER_IMAGE=neura-worker:dev``. Skipped otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from neura_runtime.runtime import RuntimeState
from neura_server.config import (
    DeploymentConfig,
    DeviceConfig,
    GatewayConfig,
    SandboxConfig,
    ServingConfig,
    token_digest,
)
from neura_server.grpc_server import GatewayServer
from neura_server.sandbox import LABEL, ContainerLauncher, Invocation
from neura_sim.grpc_robots import SimulatedRobotProcess, load_binding

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "contracts"
ARTIFACT = EXAMPLES / "act-so101.artifact.yaml"
ROBOT = EXAMPLES / "so101.robot.yaml"
PROFILE = EXAMPLES / "act-so101-30hz.profile.yaml"
IMAGE = os.environ.get("NEURA_TEST_WORKER_IMAGE", "")


def docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


def image_id() -> str | None:
    if not IMAGE or shutil.which("docker") is None:
        return None
    try:
        return docker("image", "inspect", "--format", "{{.Id}}", IMAGE)
    except (subprocess.SubprocessError, OSError):
        return None


IMAGE_ID = image_id()
pytestmark = pytest.mark.skipif(
    IMAGE_ID is None, reason="needs Docker and NEURA_TEST_WORKER_IMAGE (a neura-worker image)"
)


def sandbox() -> SandboxConfig:
    assert IMAGE_ID is not None
    return SandboxConfig(kind="container", image_override=IMAGE_ID, memory_mib=1024, cpus=1)


def workers() -> list[str]:
    return docker("ps", "--all", "--quiet", "--filter", f"label={LABEL}").split()


@asynccontextmanager
async def site(tmp_path: Path) -> AsyncIterator[tuple[GatewayServer, list[SimulatedRobotProcess]]]:
    names = ["robot-0", "robot-1"]
    config = GatewayConfig(
        listen="127.0.0.1:0",
        status_listen=None,
        devices=[DeviceConfig(robot_id=n, token_sha256=token_digest(f"token-{n}")) for n in names],
        deployments=[
            DeploymentConfig(
                revision_id="rev_test",
                artifact=ARTIFACT,
                robot_profile=ROBOT,
                execution_profile=PROFILE,
                robots=names,
                adapter="mock",
                device="cpu",
            )
        ],
        serving=ServingConfig(
            backend="workers", sandbox=sandbox(), worker_dir=tmp_path / "w", worker_timeout_ms=2000
        ),
    )
    server = GatewayServer(config)
    await server.start()
    binding = load_binding(ARTIFACT, ROBOT, PROFILE)
    target = f"127.0.0.1:{server.bound_port}"
    robots = [SimulatedRobotProcess(binding, n, "rev_test", target, f"token-{n}") for n in names]
    stop = asyncio.Event()
    tasks = [asyncio.create_task(r.connection.run(stop)) for r in robots]
    try:
        yield server, robots
    finally:
        stop.set()
        await asyncio.gather(*tasks)
        await server.stop()


async def until(what: str, check: Callable[[], bool], timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.1)


def group(server: GatewayServer) -> dict[str, Any]:
    status: dict[str, Any] = next(iter(server.status()["groups"].values()))
    return status


def chunks(server: GatewayServer) -> int:
    return sum(int(s["chunks"]) for s in server.status()["sessions"])


async def test_robots_are_served_by_a_locked_down_container_worker(tmp_path: Path) -> None:
    async with site(tmp_path) as (server, robots):
        await until("the worker", lambda: group(server)["worker"]["state"] == "ready")
        await until("chunks", lambda: chunks(server) >= 6)
        await until(
            "robots running", lambda: all(r.runtime.state is RuntimeState.RUNNING for r in robots)
        )
        [container] = workers()
        inspected = json.loads(docker("inspect", container))[0]
        host = inspected["HostConfig"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["Memory"] == 1024 * 2**20
        assert host["NanoCpus"] == 10**9
        assert host["PidsLimit"] == 512
        assert inspected["Config"]["User"] == f"{os.getuid()}:{os.getgid()}"
        assert inspected["Image"] == IMAGE_ID
        mounts = {m["Destination"]: m["RW"] for m in inspected["Mounts"]}
        assert mounts.pop("/neura/out") is True  # the directory made for this run
        assert mounts
        assert not any(mounts.values())  # every input read-only

        # A worker container that dies is replaced; robots are served again.
        docker("kill", container)
        await until("a restart", lambda: group(server)["worker"]["restarts"] >= 1)
        await until("the worker again", lambda: group(server)["worker"]["state"] == "ready")
        before = chunks(server)

        def served() -> bool:
            # A robot that ran dry while its worker was down stops itself, possibly only after
            # the worker is back (design §7); its operator resets it whenever it does.
            for robot in robots:
                if robot.runtime.state is RuntimeState.FAULTED:
                    robot.runtime.reset(time.monotonic_ns() // 1000, revalidated=True)
            return chunks(server) > before

        await until("chunks after the restart", served)
        assert len(workers()) == 1
    assert workers() == []  # stopping the gateway removes its containers


async def test_a_worker_sees_only_its_own_socket_never_another_workers(tmp_path: Path) -> None:
    # Two models, two batch groups, two containers running as the same user: neither may reach
    # the other's socket, or it could answer for the other model.
    shared = tmp_path / "w"
    deployments = [
        DeploymentConfig(
            revision_id=f"rev_{model}",
            artifact=EXAMPLES / f"{model}-so101.artifact.yaml",
            robot_profile=ROBOT,
            execution_profile=EXAMPLES / f"{model}-so101-30hz.profile.yaml",
            robots=[f"robot-{model}"],
            adapter="mock",
            device="cpu",
        )
        for model in ("act", "smolvla")
    ]
    server = GatewayServer(
        GatewayConfig(
            listen="127.0.0.1:0",
            status_listen=None,
            devices=[
                DeviceConfig(robot_id=d.robots[0], token_sha256=token_digest(d.revision_id))
                for d in deployments
            ],
            deployments=deployments,
            serving=ServingConfig(backend="workers", sandbox=sandbox(), worker_dir=shared),
        )
    )
    await server.start()
    try:
        await until(
            "both workers",
            lambda: (
                [g["worker"]["state"] for g in server.status()["groups"].values()]
                == ["ready", "ready"]
            ),
        )
        containers = workers()
        assert len(containers) == 2
        writable = set()
        for container in containers:
            inspected = json.loads(docker("inspect", container))[0]
            (mount,) = [m for m in inspected["Mounts"] if m["RW"]]
            writable.add(mount["Source"])
            listed = docker(
                "exec", container, "python", "-c", "import os; print(os.listdir('/neura/out'))"
            )
            assert listed == "['sock']"  # its own socket, nothing else
        assert len(writable) == 2
        assert str(shared.resolve()) not in writable  # never the directory every worker shares
    finally:
        await server.stop()
    assert workers() == []
    assert list(shared.iterdir()) == []


async def test_inside_the_sandbox_there_is_no_network_no_writable_root_and_no_root_user(
    tmp_path: Path,
) -> None:
    probe = (
        "import json, os, socket\n"
        "result = {'uid': os.getuid()}\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=2); result['network'] = True\n"
        "except OSError:\n"
        "    result['network'] = False\n"
        "for path in ('/etc/neura-probe', '/app/neura-probe', '/tmp/neura-probe'):\n"
        "    try:\n"
        "        open(path, 'w').write('x'); result[path] = True\n"
        "    except OSError:\n"
        "        result[path] = False\n"
        "print(json.dumps(result))\n"
    )
    launcher = ContainerLauncher(
        sandbox().model_copy(update={"entrypoint": ["python", "-c", probe]}), IMAGE_ID or ""
    )
    (tmp_path / "input.txt").write_text("read me")
    invocation = Invocation(
        "probe", "cpu", inputs=[("--input", tmp_path / "input.txt")], outputs=[]
    )
    done = subprocess.run(
        launcher.command("probe", invocation),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert result == {
        "uid": os.getuid(),
        "network": False,
        "/etc/neura-probe": False,
        "/app/neura-probe": False,
        "/tmp/neura-probe": True,
    }
