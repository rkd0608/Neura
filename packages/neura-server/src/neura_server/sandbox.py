"""Running policy workers (design §12): customer model code is untrusted executable code.

A worker run is described by an :class:`Invocation`: its subcommand, plain flags, the host paths
it reads, and the one directory it may write (its gRPC socket's, a job's output). That directory is
made for the run alone (:func:`private_directory`) and removed after it: runs never share a
writable directory, since every container runs as the same user. A launcher turns the invocation
into a command line:

- :class:`ProcessLauncher` runs ``neura-worker`` as a child process (development and tests).
- :class:`ContainerLauncher` runs it in the artifact's runtime image, pinned by digest. The
  container has no network (the scheduler reaches it over a Unix socket in a mounted directory),
  a read-only root filesystem with a small tmpfs, no capabilities and no privilege escalation,
  memory, CPU and process limits, only the device it was placed on, its inputs mounted read-only
  and only its own output directory writable. Images are pulled at prepare
  (:func:`ensure_image`), never when a worker starts.

The container's stdin is attached, so a worker still exits when its supervisor closes stdin; a
container whose supervisor is gone is removed by name (:meth:`ContainerLauncher.remove`).

What a run prints can quote the customer's code or data, which never leave the site: of a run
that failed, only how it ended is reported (:func:`failed_run`), and what it printed is logged at
the site.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import secrets
import shutil
import signal
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Protocol

from neura_server.config import SandboxConfig
from neura_server.supervision import CONTAINER_REMOVE_TIMEOUT_S

log = logging.getLogger("neura.gateway.sandbox")

LABEL = "dev.neura.worker"
OUTPUT = "/neura/out"  # where a container sees its run's directory
_CUDA = re.compile(r"^cuda:(\d+)$")


@dataclass(frozen=True, slots=True)
class Invocation:
    subcommand: str
    device: str
    flags: list[str] = field(default_factory=list)
    # flag -> a host file or directory the worker reads.
    inputs: list[tuple[str, Path]] = field(default_factory=list)
    # The one directory the worker may write, made for this run (private_directory).
    output_dir: Path | None = None
    # flag -> a name inside output_dir the worker creates ("" for the directory itself).
    outputs: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.outputs and self.output_dir is None:
            raise ValueError("a run that writes needs a directory of its own (output_dir)")
        for flag, name in self.outputs:
            if name and (len(PurePath(name).parts) != 1 or name in (".", "..")):
                raise ValueError(f"{flag} {name!r} is not a name inside the run's directory")

    def host_args(self) -> list[str]:
        args = [self.subcommand, *self.flags]
        for flag, path in self.inputs:
            args += [flag, str(path)]
        for flag, name in self.outputs:
            assert self.output_dir is not None
            args += [flag, str(self.output_dir / name)]
        return args


def private_directory(path: Path) -> Path:
    """A fresh directory only its owner may enter, for one run to write in; whatever an earlier
    run left at the same path is removed first. The caller removes it when the run is over."""
    shutil.rmtree(path, ignore_errors=True)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    path.chmod(0o700)  # whatever the umask
    return path


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
        if invocation.output_dir is not None:
            args += ["--volume", f"{invocation.output_dir.resolve()}:{OUTPUT}:rw"]
        for flag, name in invocation.outputs:
            inner += [flag, f"{OUTPUT}/{name}" if name else OUTPUT]
        return [*args, self.image, *c.entrypoint, *inner]

    async def remove(self, name: str) -> None:
        """Remove the worker's container if it is still there (its supervisor was killed, or
        the worker hung)."""
        command = [*self.config.runtime, "rm", "--force", self.container_name(name)]
        await _run(command, CONTAINER_REMOVE_TIMEOUT_S)


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


def exit_status(code: int | None) -> str:
    """How a process ended, as people say it: "exited with status 1", "was killed by SIGABRT"."""
    if code is not None and code < 0:
        try:
            return f"was killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"was killed by signal {-code}"
    return f"exited with status {code}"


def failed_run(what: str, code: int | None, output: Iterable[str], logger: logging.Logger) -> str:
    """What may be reported of a sandboxed run that failed: how it ended. What it printed (its
    own account of the failure included) goes to ``logger``, at the site, and nowhere else."""
    for line in output:
        logger.warning("%s: %s", what, line)
    return f"{what} {exit_status(code)} (its output is in the site agent's log)"


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
