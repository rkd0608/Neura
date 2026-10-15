"""Running policy workers (design §12): customer model code is untrusted executable code.

A worker run is described by an :class:`Invocation`: its subcommand, plain flags, the host paths
it reads, and the host paths it writes (its gRPC socket, a profile). A launcher turns that into a
command line:

- :class:`ProcessLauncher` runs ``neura-worker`` as a child process (development and tests).
- :class:`ContainerLauncher` runs it in the artifact's runtime image, pinned by digest. The
  container has no network (the scheduler reaches it over a Unix socket in a mounted directory),
  a read-only root filesystem with a small tmpfs, no capabilities and no privilege escalation,
  memory, CPU and process limits, only the device it was placed on, its inputs mounted read-only
  and only its output directories writable. Images are pulled at prepare
  (:func:`ensure_image`), never when a worker starts.

The container's stdin is attached, so a worker still exits when its supervisor closes stdin; a
container whose supervisor is gone is removed by name (:meth:`ContainerLauncher.remove`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from neura_server.config import SandboxConfig

log = logging.getLogger("neura.gateway.sandbox")

LABEL = "dev.neura.worker"
_CUDA = re.compile(r"^cuda:(\d+)$")


@dataclass(frozen=True, slots=True)
class Invocation:
    subcommand: str
    device: str
    flags: list[str] = field(default_factory=list)
    # flag -> a host file or directory the worker reads.
    inputs: list[tuple[str, Path]] = field(default_factory=list)
    # flag -> a host path the worker creates (its directory must exist).
    outputs: list[tuple[str, Path]] = field(default_factory=list)

    def host_args(self) -> list[str]:
        args = [self.subcommand, *self.flags]
        for flag, path in [*self.inputs, *self.outputs]:
            args += [flag, str(path)]
        return args


class Launcher(Protocol):
    def command(self, name: str, invocation: Invocation) -> list[str]: ...

    async def remove(self, name: str) -> None: ...


class ProcessLauncher:
    """A plain child process: no isolation beyond the operating system user."""

    def __init__(self, command: Sequence[str]) -> None:
        self._command = list(command)

    def command(self, name: str, invocation: Invocation) -> list[str]:
        del name
        return [*self._command, *invocation.host_args()]

    async def remove(self, name: str) -> None:
        del name


class ContainerLauncher:
    def __init__(self, config: SandboxConfig, image: str) -> None:
        self.config = config
        self.image = config.image_override or image
        self.instance = secrets.token_hex(4)  # container names are unique per launcher

    def container_name(self, name: str) -> str:
        return f"neura-worker-{self.instance}-{name}"

    def _user(self) -> str:
        return self.config.user or f"{os.getuid()}:{os.getgid()}"

    def command(self, name: str, invocation: Invocation) -> list[str]:
        c = self.config
        args = [
            *c.runtime,
            "run",
            "--rm",
            "--interactive",
            "--name",
            self.container_name(name),
            "--label",
            f"{LABEL}={self.instance}",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={c.tmp_mib}m",  # noqa: S108 - the container's own tmpfs
            "--shm-size",
            f"{c.shm_mib}m",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(c.pids_limit),
            "--user",
            self._user(),
            "--env",
            "HOME=/tmp",
            "--env",
            "XDG_CACHE_HOME=/tmp/cache",
            "--env",
            "HF_HUB_OFFLINE=1",
        ]
        if c.memory_mib is not None:
            args += ["--memory", f"{c.memory_mib}m", "--memory-swap", f"{c.memory_mib}m"]
        if c.cpus is not None:
            args += ["--cpus", f"{c.cpus:g}"]
        cuda = _CUDA.match(invocation.device)
        if cuda:
            args += [a.format(index=cuda.group(1)) for a in c.gpu_args]
        inner = [invocation.subcommand, *invocation.flags]
        for i, (flag, path) in enumerate(invocation.inputs):
            target = f"/neura/in/{i}/{path.name}"
            args += ["--volume", f"{path.resolve()}:{target}:ro"]
            inner += [flag, target]
        for i, (flag, path) in enumerate(invocation.outputs):
            directory = f"/neura/out/{i}"
            args += ["--volume", f"{path.parent.resolve()}:{directory}:rw"]
            inner += [flag, f"{directory}/{path.name}"]
        return [*args, self.image, *c.entrypoint, *inner]

    async def remove(self, name: str) -> None:
        """Remove the worker's container if it is still there (its supervisor was killed, or
        the worker hung)."""
        await _run([*self.config.runtime, "rm", "--force", self.container_name(name)], 30)


async def _run(command: list[str], timeout_s: float) -> tuple[int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, str(exc)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return 124, f"{command[0]} did not finish in {timeout_s:.0f} s"
    return process.returncode or 0, output.decode(errors="replace").strip()


class ImageUnavailableError(Exception):
    pass


async def ensure_image(runtime: Sequence[str], image: str, *, pull_timeout_s: float) -> None:
    """Make the pinned image present locally, pulling it (by digest) if needed. Raises
    ImageUnavailableError with the runtime's message."""
    code, _ = await _run([*runtime, "image", "inspect", image], 60)
    if code == 0:
        return
    if image.startswith("sha256:"):
        raise ImageUnavailableError(f"image {image} is not present and cannot be pulled by id")
    code, output = await _run([*runtime, "pull", image], pull_timeout_s)
    if code != 0:
        raise ImageUnavailableError(f"cannot pull {image}: {output[-500:]}")


async def remove_stale(runtime: Sequence[str]) -> None:
    """Remove worker containers left by a gateway that did not stop cleanly."""
    code, output = await _run(
        [*runtime, "ps", "--all", "--quiet", "--filter", f"label={LABEL}"], 30
    )
    ids = output.split() if code == 0 else []
    if ids:
        log.warning("removing %d worker containers left from an earlier run", len(ids))
        await _run([*runtime, "rm", "--force", *ids], 60)


def launcher_for(sandbox: SandboxConfig, worker_command: Sequence[str], image: str) -> Launcher:
    if sandbox.kind == "container":
        return ContainerLauncher(sandbox, image)
    return ProcessLauncher(worker_command)
