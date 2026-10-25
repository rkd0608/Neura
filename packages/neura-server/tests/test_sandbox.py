"""The worker sandbox (design §12): what a container worker may and may not do, and removing its
container when it stops."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from neura_server.backends import WorkerBackend, WorkerSpec
from neura_server.config import SandboxConfig
from neura_server.sandbox import ContainerLauncher, Invocation, ProcessLauncher, launcher_for

IMAGE = "registry.example/neura/act@sha256:" + "a" * 64


def spec(tmp_path: Path, device: str = "cuda:1") -> WorkerSpec:
    for name in ("artifact.yaml", "robot.yaml", "profile.yaml"):
        (tmp_path / name).write_text("{}")
    (tmp_path / "checkpoint").mkdir()
    return WorkerSpec(
        artifact=tmp_path / "artifact.yaml",
        robot_profile=tmp_path / "robot.yaml",
        execution_profile=tmp_path / "profile.yaml",
        device=device,
        checkpoint=tmp_path / "checkpoint",
        warmup_batches=(1, 8),
    )


def flag(args: list[str], name: str) -> list[str]:
    return [args[i + 1] for i, a in enumerate(args) if a == name]


def test_a_container_worker_is_locked_down(tmp_path: Path) -> None:
    sandbox = SandboxConfig(kind="container", memory_mib=8192, cpus=4, user="10001:10001")
    launcher = ContainerLauncher(sandbox, IMAGE)
    socket = tmp_path / "run" / "g1-1.sock"
    args = launcher.command("g1-1", spec(tmp_path).invocation(socket))
    assert args[:2] == ["docker", "run"]
    for required in ("--rm", "--interactive", "--read-only"):
        assert required in args
    assert flag(args, "--network") == ["none"]
    assert flag(args, "--pull") == ["never"]  # images are fetched at prepare, never here
    assert flag(args, "--cap-drop") == ["ALL"]
    assert flag(args, "--security-opt") == ["no-new-privileges"]
    assert flag(args, "--memory") == ["8192m"]
    assert flag(args, "--memory-swap") == ["8192m"]
    assert flag(args, "--cpus") == ["4"]
    assert flag(args, "--pids-limit") == ["512"]
    assert flag(args, "--user") == ["10001:10001"]
    assert flag(args, "--gpus") == ["device=1"]  # only the device it was placed on
    assert flag(args, "--name") == [launcher.container_name("g1-1")]
    volumes = flag(args, "--volume")
    # Inputs read-only, only the socket's directory writable.
    assert sum(v.endswith(":ro") for v in volumes) == 4
    assert [v for v in volumes if v.endswith(":rw")] == [
        f"{(tmp_path / 'run').resolve()}:/neura/out/0:rw"
    ]
    image_at = args.index(IMAGE)
    inner = args[image_at + 1 :]
    assert inner[:2] == ["neura-worker", "serve"]
    assert flag(inner, "--socket") == ["/neura/out/0/g1-1.sock"]
    assert flag(inner, "--artifact") == ["/neura/in/0/artifact.yaml"]
    assert flag(inner, "--checkpoint") == ["/neura/in/3/checkpoint"]
    assert flag(inner, "--device") == ["cuda:1"]
    assert str(tmp_path) not in " ".join(inner)  # no host path inside the container


def test_cpu_workers_get_no_gpu_and_the_override_image_is_used(tmp_path: Path) -> None:
    override = "sha256:" + "b" * 64
    sandbox = SandboxConfig(kind="container", runtime=["podman"], image_override=override)
    args = ContainerLauncher(sandbox, IMAGE).command(
        "g1-1", spec(tmp_path, device="cpu").invocation(tmp_path / "s.sock")
    )
    assert args[0] == "podman"
    assert "--gpus" not in args
    assert override in args
    assert IMAGE not in args
    assert "--memory" not in args  # no limit unless configured


def test_the_process_launcher_runs_the_worker_command_with_host_paths(tmp_path: Path) -> None:
    launcher = launcher_for(SandboxConfig(), ["python", "-m", "neura_server.worker"], IMAGE)
    assert isinstance(launcher, ProcessLauncher)
    args = launcher.command("g1-1", spec(tmp_path).invocation(tmp_path / "s.sock"))
    assert args[:4] == ["python", "-m", "neura_server.worker", "serve"]
    assert flag(args, "--socket") == [str(tmp_path / "s.sock")]
    assert flag(args, "--checkpoint") == [str(tmp_path / "checkpoint")]


class SlowToRemove:
    """A worker that exits by itself, and a container runtime slow to remove what it leaves."""

    def __init__(self) -> None:
        self.removed: list[str] = []

    def command(self, name: str, invocation: Invocation) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(0.2)"]

    async def remove(self, name: str) -> None:
        await asyncio.sleep(1.0)
        self.removed.append(name)


async def test_stopping_a_worker_waits_until_its_container_is_removed(tmp_path: Path) -> None:
    launcher = SlowToRemove()
    backend = WorkerBackend(
        "g1", spec(tmp_path, "cpu"), socket_dir=tmp_path, start_timeout_s=30, launcher=launcher
    )
    await backend.start()
    await asyncio.sleep(0.6)  # the worker has exited; its container is being removed
    await backend.stop()
    # A gateway exits once its workers are stopped: a removal still under way would never finish
    # and leave the container running.
    assert launcher.removed == ["g1-1"]
