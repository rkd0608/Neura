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
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from neura_adapters import ModelInputs, PolicyError
from neura_adapters.base import blank_inputs
from neura_bench.deployments import DeploymentFiles
from neura_bench.sim import Contention, LatencyModel
from neura_runtime.binding import ExecutionBinding
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
) -> AsyncIterator[list[WorkerBackend]]:
    """Real worker processes, one per deployment, all on ``device``, ready to serve."""
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
                warmup_batches=tuple(sorted({1, max_batch})),
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

    def to_json(self) -> dict[str, Any]:
        return {
            "points": [
                {
                    "batch_size": p.batch_size,
                    "p50_ms": round(p.p50_us / 1000, 3),
                    "p99_ms": round(p.p99_us / 1000, 3),
                    "memory_mib": p.memory_mib,
                    "samples": self.samples[p.batch_size],
                }
                for p in self.model.points
            ]
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
    sizes run in increasing order, so the peak after each size is that size's)."""
    sample = sample_inputs(files.binding)
    points = []
    samples = {}
    async with workers([files], device, max_batch=max(batch_sizes)) as (backend,):
        for size in sorted(set(batch_sizes)):
            batch = [sample] * size
            for i in range(3):
                await _timed(backend, batch, i)
            timings = await _loop(backend, batch, seconds=seconds, minimum=minimum)
            points.append(_point(size, timings, backend.peak_memory_mib))
            samples[size] = len(timings)
    return LatencyMeasurement(LatencyModel(tuple(points)), samples)


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
) -> ContentionMeasurement:
    """Slowdown with k = 1..len(deployments) workers busy on the device at batch size 1, and
    with a background job (``background`` is the command that runs it)."""
    sample = sample_inputs(deployments[0].binding)
    results: dict[int, dict[str, float]] = {}
    async with workers(deployments, device, max_batch=1) as backends:
        for backend in backends:
            for i in range(3):
                await _timed(backend, [sample], i)
        for k in range(1, len(backends) + 1):
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
