"""Where a revision's batch group runs, and what it measures there (design §9.4, §9.5, §13).

A batch group (artifact, precision) is placed on one device and stays there: revisions of the
same artifact join its group, so their robots share forward passes. A new group goes to the
allowed GPU with the fewest groups. At prepare, the group's worker is loaded on that device and
profiled (`neura-worker profile`); the gateway's scheduler and admission control, and the
capacity estimate reported to the cloud, then use the measured profile.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import signal
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neura_agent.artifacts import ArtifactError
from neura_contracts import DeviceKind, ReasonCode
from neura_runtime.binding import ExecutionBinding
from neura_server.config import profile_batch_sizes
from neura_server.sandbox import Invocation, Launcher

log = logging.getLogger("neura.agent")

SAME_WEIGHTS = "same_weights"
CO_LOCATED_FINE_TUNES = "co_located_fine_tunes"
DIFFERENT_ARCHITECTURES = "different_architectures"


@dataclass(frozen=True, slots=True)
class PlacedGroup:
    key: str
    device: str
    adapter: str


def group_key(binding: ExecutionBinding) -> str:
    return f"{binding.artifact_digest}:{binding.profile.spec.runtime.precision.value}"


def choose_device(
    binding: ExecutionBinding, placed: Iterable[PlacedGroup], cuda_indexes: list[int]
) -> str:
    """The group's existing device, else the least-loaded allowed device of the right kind."""
    groups = list(placed)
    key = group_key(binding)
    for group in groups:
        if group.key == key:
            return group.device
    if binding.profile.spec.runtime.device is DeviceKind.CPU:
        return "cpu"
    if not cuda_indexes:
        raise ArtifactError(
            ReasonCode.AGENT_DEVICE_UNAVAILABLE,
            "the execution profile needs a CUDA GPU and this agent may use none "
            "(check nvidia-smi and NEURA_AGENT_ALLOWED_GPU_INDEXES)",
        )
    load = {f"cuda:{i}": 0 for i in sorted(cuda_indexes)}
    for group in groups:
        if group.device in load:
            load[group.device] += 1
    return min(load, key=lambda d: (load[d], int(d.split(":")[1])))


def sharing_case(binding: ExecutionBinding, device: str, placed: Iterable[PlacedGroup]) -> str:
    """Which sharing case of §9.5 applies to this revision's group on its device."""
    key = group_key(binding)
    adapter = binding.artifact.spec.adapter.name
    others = [g for g in placed if g.device == device and g.key != key]
    if not others:
        return SAME_WEIGHTS
    if all(g.adapter == adapter for g in others):
        return CO_LOCATED_FINE_TUNES
    return DIFFERENT_ARCHITECTURES


def profile_path(
    directory: Path, binding: ExecutionBinding, device: str, max_batch: int, adapter: str
) -> Path:
    """Measured profiles are kept per (artifact, precision, device, batch sizes, adapter)."""
    key = f"{group_key(binding)}|{device}|{max_batch}|{adapter}"
    name = hashlib.sha256(key.encode()).hexdigest()[:24]
    return directory / f"{name}.json"


async def measure(
    launcher: Launcher,
    *,
    documents: dict[str, Path],
    device: str,
    checkpoint: Path | None,
    out: Path,
    max_batch: int,
    iterations: int,
    timeout_s: float,
    adapter: str | None = None,
) -> dict[str, Any]:
    """Run `neura-worker profile` (in the worker sandbox) and return the measured profile.
    Raises ArtifactError with the worker's reason when the model cannot be loaded or profiled
    on the device."""
    batches = ",".join(str(b) for b in profile_batch_sizes(max_batch))
    flags = ["--device", device, "--batch-sizes", batches, "--warmup", batches]
    flags += ["--iterations", str(iterations)]
    if adapter is not None:
        flags += ["--adapter", adapter]
    inputs = [
        ("--artifact", documents["model_artifact"]),
        ("--robot-profile", documents["robot_profile"]),
        ("--execution-profile", documents["execution_profile"]),
    ]
    if checkpoint is not None:
        inputs.append(("--checkpoint", checkpoint))
    invocation = Invocation("profile", device, flags, inputs, [("--out", out)])
    name = f"profile-{secrets.token_hex(4)}"
    args = launcher.command(name, invocation)
    out.parent.mkdir(parents=True, exist_ok=True)
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ArtifactError(
            ReasonCode.WORKER_LOAD_FAILED, f"profiling did not finish in {timeout_s:.0f} s"
        ) from None
    finally:
        await launcher.remove(name)
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and "profile" in message:
            profile: dict[str, Any] = message["profile"]
            return profile
        if isinstance(message, dict) and "error" in message:
            error = message["error"]
            try:
                code = ReasonCode(error.get("code"))
            except ValueError:
                code = ReasonCode.WORKER_LOAD_FAILED
            raise ArtifactError(code, str(error.get("message", "")))
    # The worker died without saying why. Its output stays in the site's log: it can quote the
    # customer's code, which does not leave the site.
    for line in stderr.decode(errors="replace").splitlines()[-40:]:
        log.warning("profiler: %s", line)
    raise ArtifactError(
        ReasonCode.WORKER_LOAD_FAILED,
        f"the profiler {exit_status(process.returncode)} (its output is in the site agent's log)",
    )


def exit_status(code: int | None) -> str:
    """How a process ended, as people say it: "exited with status 1", "was killed by SIGABRT"."""
    if code is not None and code < 0:
        try:
            return f"was killed by {signal.Signals(-code).name}"
        except ValueError:
            return f"was killed by signal {-code}"
    return f"exited with status {code}"
