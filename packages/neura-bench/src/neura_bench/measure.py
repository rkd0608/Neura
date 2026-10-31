"""Measurements on the benchmark device, through the same worker processes production runs.

- latency per batch size, as the gateway sees it: a `WorkerBackend` drives a real
  `neura-worker` over its Unix socket, so serialization and IPC are included;
- co-location contention: k worker processes on one device, each running batches back to back;
  slowdown(k) is the median latency with k running divided by the median alone;
- a background job: the same with a non-policy compute job running on the device;
- multi-adapter batching: the forward pass of a batch mixing LoRA adapters of one base model,
  against the base model alone, with the parity check (design §9.5).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_adapters.base import blank_inputs
from neura_bench.deployments import DeploymentFiles
from neura_bench.sim import Contention, LatencyModel
from neura_runtime.binding import ExecutionBinding
from neura_runtime.policy import ModelInputs, PolicyError
from neura_server.backends import WorkerBackend, WorkerSpec
from neura_server.profiles import ProfilePoint

WORKER = [sys.executable, "-m", "neura_server.worker"]


def sample_inputs(binding: ExecutionBinding, seed: int = 0) -> ModelInputs:
    """Inputs of the right shapes with random content (blank images can take fast paths)."""
    rng = np.random.default_rng(seed)
    blank = blank_inputs(binding)
    return ModelInputs(
        state={k: rng.normal(0, 20, v.shape).astype(np.float32) for k, v in blank.state.items()},
        images={k: rng.integers(0, 255, v.shape, dtype=np.uint8) for k, v in blank.images.items()},
        instruction=blank.instruction,
        frozen_prefix=None,
    )


@contextlib.asynccontextmanager
async def workers(
    deployments: Sequence[DeploymentFiles],
    device: str,
    *,
    max_batch: int,
    adapter: str | None = None,
    warmup: Sequence[int] | None = None,
) -> AsyncIterator[list[WorkerBackend]]:
    """Real worker processes, one per deployment, all on ``device``, ready to serve. They warm
    up on batches of ``warmup`` (default: of 1 and ``max_batch``)."""
    directory = Path(tempfile.mkdtemp(prefix="neura-b-"))
    backends = [
        WorkerBackend(
            f"b{i}",
            WorkerSpec(
                artifact=files.artifact,
                robot_profile=files.robot_profile,
                execution_profile=files.execution_profile,
                device=device,
                checkpoint=files.checkpoint,
                adapter=adapter,
                warmup_batches=tuple(sorted({1, max_batch} if warmup is None else warmup)),
            ),
            command=WORKER,
            socket_dir=directory,
            start_timeout_s=900,
        )
        for i, files in enumerate(deployments)
    ]
    try:
        for backend in backends:
            await backend.start()
        for backend in backends:
            while backend.state != "ready":
                if backend.state == "failed":
                    raise PolicyError(f"worker could not start: {backend.last_error}")
                await asyncio.sleep(0.05)
        yield backends
    finally:
        await asyncio.gather(*(b.stop() for b in backends))
        with contextlib.suppress(OSError):
            directory.rmdir()


async def _timed(backend: WorkerBackend, batch: list[ModelInputs], batch_id: int) -> int:
    start = time.perf_counter_ns()
    await backend.infer(batch_id, batch, timeout_s=120)
    return (time.perf_counter_ns() - start) // 1000


async def _loop(
    backend: WorkerBackend, batch: list[ModelInputs], *, seconds: float, minimum: int
) -> list[int]:
    """Batches back to back for ``seconds`` (and at least ``minimum`` of them)."""
    timings: list[int] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline or len(timings) < minimum:
        timings.append(await _timed(backend, batch, len(timings) + 1))
    return timings


def _point(batch_size: int, timings: list[int], memory_mib: int) -> ProfilePoint:
    values = np.array(timings, dtype=np.int64)
    p50 = int(np.percentile(values, 50))
    return ProfilePoint(batch_size, p50, max(int(np.percentile(values, 99)), p50), memory_mib)


@dataclass(frozen=True, slots=True)
class LatencyMeasurement:
    model: LatencyModel
    samples: dict[int, int]
    # The smallest batch size that did not fit in the device's memory, if one was tried.
    out_of_memory_at: int | None = None
    # Where the worker's whole device memory was read: "process" (the driver's accounting of
    # the process), "device" (the device's use with the worker alone on it, less its use before
    # the worker started), or None (a CPU, or a driver that reported neither).
    overhead_source: str | None = None

    @property
    def largest_batch(self) -> int:
        return max(p.batch_size for p in self.model.points)

    def to_json(self) -> dict[str, Any]:
        return {
            "process_overhead_mib": self.model.process_overhead_mib,
            "process_overhead_source": self.overhead_source,
            "out_of_memory_at": self.out_of_memory_at,
            "points": [
                {
                    "batch_size": p.batch_size,
                    "p50_ms": round(p.p50_us / 1000, 3),
                    "p99_ms": round(p.p99_us / 1000, 3),
                    "memory_mib": p.memory_mib,
                    "samples": self.samples[p.batch_size],
                }
                for p in self.model.points
            ],
        }


async def latency(
    files: DeploymentFiles,
    device: str,
    *,
    batch_sizes: Sequence[int],
    seconds: float,
    minimum: int = 20,
) -> LatencyMeasurement:
    """Latency per batch size of one worker alone on the device, and its peak memory (batch
    sizes run in increasing order, so the peak after each size is that size's). The sizes stop at
    the first that does not fit in the device's memory. On a GPU, the worker's whole footprint
    is read from the driver after each size, for its overhead beyond the peak allocation."""
    sample = sample_inputs(files.binding)
    points: list[ProfilePoint] = []
    samples = {}
    out_of_memory_at = None
    footprint: int | None = None
    source: str | None = None
    baseline = device_used_mib(device)
    async with workers([files], device, max_batch=max(batch_sizes), warmup=(1,)) as (backend,):
        for size in sorted(set(batch_sizes)):
            batch = [sample] * size
            try:
                for i in range(3):
                    await _timed(backend, batch, i)
                timings = await _loop(backend, batch, seconds=seconds, minimum=minimum)
            except PolicyError as exc:
                if not exc.out_of_memory or not points:
                    raise
                out_of_memory_at = size
                break
            points.append(_point(size, timings, backend.peak_memory_mib))
            samples[size] = len(timings)
            footprint, source = _footprint(backend.status().get("pid"), device, baseline)
    overhead = max(0, footprint - points[-1].memory_mib) if footprint is not None else 0
    model = LatencyModel(tuple(points), process_overhead_mib=overhead)
    return LatencyMeasurement(model, samples, out_of_memory_at, source)


def _footprint(pid: int | None, device: str, baseline: int | None) -> tuple[int | None, str | None]:
    """A worker's whole device memory, and where it was read: the driver's accounting of the
    process or, where the driver cannot match the process (a container with its own process
    IDs reports none), the device's use less its use before the worker started."""
    own = process_memory_mib(pid)
    if own is not None:
        return own, "process"
    used = device_used_mib(device)
    if used is not None and baseline is not None:
        return max(0, used - baseline), "device"
    return None, None


def _nvidia_smi(*args: str) -> str | None:
    """The NVIDIA driver's tool's output, or None without one."""
    tool = shutil.which("nvidia-smi")
    if tool is None:
        return None
    try:
        return subprocess.run(  # noqa: S603 - the driver's tool, fixed arguments
            [tool, *args], capture_output=True, text=True, timeout=10, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def process_memory_mib(pid: int | None) -> int | None:
    """A process's device memory as the NVIDIA driver accounts it (context and caches
    included), or None without one."""
    if pid is None:
        return None
    out = _nvidia_smi("--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits")
    for line in (out or "").splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) == 2 and fields[0] == str(pid) and fields[1].isdigit():
            return int(fields[1])
    return None


def _gpu_mib(device: str, field: str) -> int | None:
    if not device.startswith("cuda"):
        return None
    index = device.partition(":")[2] or "0"
    out = _nvidia_smi(f"--query-gpu={field}", "--format=csv,noheader,nounits", "-i", index)
    text = (out or "").strip()
    return int(text) if text.isdigit() else None


def device_memory_mib(device: str) -> int | None:
    """The device's memory, from the NVIDIA driver (no CUDA context in this process), or None
    for a CPU."""
    return _gpu_mib(device, "memory.total")


def device_used_mib(device: str) -> int | None:
    """The device's memory in use by every process on it, from the NVIDIA driver, or None for a
    CPU."""
    return _gpu_mib(device, "memory.used")


@dataclass(frozen=True, slots=True)
class ContentionMeasurement:
    contention: Contention
    alone_p50_ms: float
    by_workers: dict[int, dict[str, float]]

    def to_json(self) -> dict[str, Any]:
        return {
            "alone_p50_ms": self.alone_p50_ms,
            "slowdown": {str(k): v for k, v in self.contention.slowdown},
            "background_slowdown": self.contention.background,
            "by_workers": {str(k): v for k, v in self.by_workers.items()},
        }


async def contention(
    deployments: Sequence[DeploymentFiles],
    device: str,
    *,
    seconds: float,
    background: Sequence[str] | None = None,
    counts: Sequence[int] = (),
) -> ContentionMeasurement:
    """Slowdown with k workers busy on the device at batch size 1, for each k in ``counts``
    (default 1..len(deployments); 1 is always measured), and with a background job
    (``background`` is the command that runs it)."""
    ks = sorted({1, *counts}) if counts else list(range(1, len(deployments) + 1))
    if ks[-1] > len(deployments):
        raise ValueError(f"{ks[-1]} workers asked for, {len(deployments)} deployments given")
    sample = sample_inputs(deployments[0].binding)
    results: dict[int, dict[str, float]] = {}
    async with workers(deployments, device, max_batch=1) as backends:
        for backend in backends:
            for i in range(3):
                await _timed(backend, [sample], i)
        for k in ks:
            runs = await asyncio.gather(
                *(_loop(b, [sample], seconds=seconds, minimum=10) for b in backends[:k])
            )
            values = np.concatenate([np.array(r, dtype=np.int64) for r in runs])
            results[k] = {
                "p50_ms": round(float(np.percentile(values, 50)) / 1000, 3),
                "p99_ms": round(float(np.percentile(values, 99)) / 1000, 3),
            }
        alone = results[1]["p50_ms"]
        slowdown = tuple((k, round(v["p50_ms"] / alone, 3)) for k, v in sorted(results.items()))
        background_factor = 1.0
        if background is not None:
            job = await asyncio.create_subprocess_exec(
                *background, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL
            )
            try:
                await asyncio.sleep(1.0)  # let it reach steady state
                loaded = await _loop(backends[0], [sample], seconds=seconds, minimum=10)
            finally:
                job.terminate()
                await job.wait()
            background_factor = round(float(np.percentile(loaded, 50)) / 1000 / alone, 3)
            results[0] = {
                "p50_ms": round(float(np.percentile(loaded, 50)) / 1000, 3),
                "p99_ms": round(float(np.percentile(loaded, 99)) / 1000, 3),
            }
    return ContentionMeasurement(Contention(slowdown, background_factor), alone, results)


def background_command(device: str, seconds: float = 3600) -> list[str]:
    """A non-policy compute job on the device: matrix multiplications back to back."""
    return [
        sys.executable,
        "-m",
        "neura_bench.background",
        "--device",
        device,
        "--seconds",
        str(seconds),
    ]


@dataclass(frozen=True, slots=True)
class MultiAdapterMeasurement:
    adapters: int
    rank: int
    targets: int
    base: dict[int, float]  # batch size -> p50 ms
    mixed: dict[int, float]  # same, with every request using a different adapter
    parity_max_rel_error: float
    parity_tolerance: float
    parity_passed: bool

    def overhead(self, batch_size: int) -> float:
        return self.mixed[batch_size] / self.base[batch_size]

    def to_json(self) -> dict[str, Any]:
        return {
            "adapters": self.adapters,
            "rank": self.rank,
            "adapted_layers": self.targets,
            "base_p50_ms": {str(k): v for k, v in self.base.items()},
            "mixed_p50_ms": {str(k): v for k, v in self.mixed.items()},
            "overhead": {str(k): round(self.overhead(k), 3) for k in self.base},
            "parity": {
                "max_rel_error": self.parity_max_rel_error,
                "tolerance": self.parity_tolerance,
                "passed": self.parity_passed,
            },
        }


def multi_adapter(
    files: DeploymentFiles,
    device: str,
    *,
    batch_sizes: Sequence[int],
    rank: int = 16,
    iterations: int = 20,
) -> MultiAdapterMeasurement:
    """One ACT base model serving a batch whose requests each use a different LoRA adapter,
    against the same batch on the base model alone. In-process: the hooks act on the model."""
    from neura_adapters import load_adapter
    from neura_adapters.lora import MultiLora, called_linear_layers, check_parity, random_adapters

    adapter: Any = load_adapter(
        "lerobot-act", files.binding, checkpoint=files.checkpoint, device=device
    )
    network = adapter.policy.model
    sample = sample_inputs(files.binding)
    largest = max(batch_sizes)

    def probe(n: int) -> list[NDArray[np.float32]]:
        outputs: list[NDArray[np.float32]] = adapter.infer([sample] * n)
        return outputs

    requests = [sample_inputs(files.binding, seed) for seed in range(largest + 1)]

    def once(batch: list[ModelInputs], indices: list[int] | None) -> float:
        start = time.perf_counter_ns()
        with lora.active(indices) if indices is not None else contextlib.nullcontext():
            adapter.infer(batch)
        return (time.perf_counter_ns() - start) / 1e6

    targets = called_linear_layers(network, probe)
    lora = MultiLora(
        network, random_adapters(network, targets, count=largest, rank=rank), probe=probe
    )
    parity = check_parity(lora, lambda positions: adapter.infer([requests[p] for p in positions]))
    base: dict[int, float] = {}
    mixed: dict[int, float] = {}
    for size in sorted(set(batch_sizes)):
        batch = requests[:size]
        indices = list(range(size))
        for _ in range(3):
            once(batch, None)
            once(batch, indices)
        # Interleaved, so drift on the device (clocks, other load) affects both alike. With no
        # adapter active the hooks return at once: the base pass costs what it would without them.
        pairs = [(once(batch, None), once(batch, indices)) for _ in range(iterations)]
        base[size] = round(float(np.percentile([b for b, _ in pairs], 50)), 3)
        mixed[size] = round(float(np.percentile([m for _, m in pairs], 50)), 3)
    return MultiAdapterMeasurement(
        adapters=largest,
        rank=rank,
        targets=len(targets),
        base=base,
        mixed=mixed,
        parity_max_rel_error=parity.max_rel_error,
        parity_tolerance=parity.tolerance,
        parity_passed=parity.passed,
    )
