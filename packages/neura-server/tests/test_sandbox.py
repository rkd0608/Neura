"""The worker sandbox (design §12): what a container worker may and may not do, and removing its
container when it stops."""

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

import pytest

from neura_server.backends import WorkerBackend, WorkerSpec
from neura_server.config import SandboxConfig
from neura_server.sandbox import (
    ContainerLauncher,
    Invocation,
    ProcessLauncher,
    launcher_for,
    private_directory,
)

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
    run = tmp_path / "g1-1"
    args = launcher.command("g1-1", spec(tmp_path).invocation(run))
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
    # Inputs read-only; writable, only the directory made for this run.
    assert sum(v.endswith(":ro") for v in volumes) == 4
    assert [v for v in volumes if v.endswith(":rw")] == [f"{run.resolve()}:/neura/out:rw"]
    image_at = args.index(IMAGE)
    inner = args[image_at + 1 :]
    assert inner[:2] == ["neura-worker", "serve"]
    assert flag(inner, "--socket") == ["/neura/out/sock"]
    assert flag(inner, "--artifact") == ["/neura/in/0/artifact.yaml"]
    assert flag(inner, "--checkpoint") == ["/neura/in/3/checkpoint"]
    assert flag(inner, "--device") == ["cuda:1"]
    assert str(tmp_path) not in " ".join(inner)  # no host path inside the container


def test_cpu_workers_get_no_gpu_and_the_override_image_is_used(tmp_path: Path) -> None:
    override = "sha256:" + "b" * 64
    sandbox = SandboxConfig(kind="container", runtime=["podman"], image_override=override)
    args = ContainerLauncher(sandbox, IMAGE).command(
        "g1-1", spec(tmp_path, device="cpu").invocation(tmp_path / "g1-1")
    )
    assert args[0] == "podman"
    assert "--gpus" not in args
    assert override in args
    assert IMAGE not in args
    assert "--memory" not in args  # no limit unless configured


def test_the_process_launcher_runs_the_worker_command_with_host_paths(tmp_path: Path) -> None:
    launcher = launcher_for(SandboxConfig(), ["python", "-m", "neura_server.worker"], IMAGE)
    assert isinstance(launcher, ProcessLauncher)
    args = launcher.command("g1-1", spec(tmp_path).invocation(tmp_path / "g1-1"))
    assert args[:4] == ["python", "-m", "neura_server.worker", "serve"]
    assert flag(args, "--socket") == [str(tmp_path / "g1-1" / "sock")]
    assert flag(args, "--checkpoint") == [str(tmp_path / "checkpoint")]


def test_a_run_writes_only_inside_the_directory_made_for_it(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="directory"):
        Invocation("serve", "cpu", outputs=[("--socket", "worker.sock")])
    for name in ("../worker.sock", "a/worker.sock", ".."):
        with pytest.raises(ValueError, match="inside"):
            Invocation("serve", "cpu", output_dir=tmp_path, outputs=[("--socket", name)])
    # A directory left by an earlier run at the same path is replaced, not shared.
    (tmp_path / "run").mkdir(mode=0o755)
    (tmp_path / "run" / "left.sock").write_text("")
    made = private_directory(tmp_path / "run")
    assert list(made.iterdir()) == []
    assert stat.S_IMODE(made.stat().st_mode) == 0o700


class Recording:
    """A container launcher whose commands are recorded, not run: the worker started exits."""

    def __init__(self) -> None:
        self.launcher = ContainerLauncher(SandboxConfig(kind="container"), IMAGE)
        self.commands: dict[str, list[str]] = {}
        self.modes: dict[str, int] = {}

    def command(self, name: str, invocation: Invocation) -> list[str]:
        self.commands[name] = self.launcher.command(name, invocation)
        assert invocation.output_dir is not None
        self.modes[name] = stat.S_IMODE(invocation.output_dir.stat().st_mode)
        return [sys.executable, "-c", "pass"]

    async def remove(self, name: str) -> None:
        del name


async def test_each_worker_run_may_write_only_a_directory_of_its_own(tmp_path: Path) -> None:
    # Every batch group's and shadow's worker has its socket under the gateway's one worker
    # directory, and every container runs as the same user: a worker that could write there
    # could answer another model's requests.
    shared = tmp_path / "w"
    shared.mkdir()
    launchers = {"g1": Recording(), "s2": Recording()}
    worker = spec(tmp_path, "cpu")
    backends = [
        WorkerBackend(name, worker, socket_dir=shared, start_timeout_s=30, launcher=launcher)
        for name, launcher in launchers.items()
    ]
    for backend in backends:
        await backend.start()
    for _ in range(200):
        if all(launcher.commands for launcher in launchers.values()):
            break
        await asyncio.sleep(0.01)
    for backend in backends:
        await backend.stop()
    for name, launcher in launchers.items():
        run = f"{name}-1"
        volumes = flag(launcher.commands[run], "--volume")
        assert [v for v in volumes if v.endswith(":rw")] == [
            f"{(shared / run).resolve()}:/neura/out:rw"
        ]
        assert launcher.modes[run] == 0o700
    assert list(shared.iterdir()) == []  # each run's directory goes with it


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
