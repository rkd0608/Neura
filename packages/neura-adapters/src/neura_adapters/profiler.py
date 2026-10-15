"""Measuring an adapter on its device: latency per batch size and peak memory (design §9.2).

The scheduler and admission control use these profiles, and capacity estimates built on them say
`measured_profile`. Measure on the site's own GPU: profiles do not transfer between device
models, drivers or precisions.
"""

from __future__ import annotations

import resource
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from neura_adapters.base import Adapter, ModelInputs, blank_inputs


@dataclass(frozen=True, slots=True)
class MeasuredPoint:
    batch_size: int
    p50_us: int
    p99_us: int
    max_us: int
    memory_mib: int
    samples: int


@dataclass(frozen=True, slots=True)
class MeasuredProfile:
    adapter: str
    device: str
    precision: str
    points: tuple[MeasuredPoint, ...]
    # How memory was measured: "cuda_peak_allocated" (device memory the model's tensors used) or
    # "process_peak_rss" (on CPU: the whole process, an upper bound).
    memory_source: str

    def to_json(self) -> dict[str, object]:
        return {**asdict(self), "points": [asdict(p) for p in self.points]}


def _cuda(device: str) -> Any:
    """torch.cuda when the adapter runs on a CUDA device, else None."""
    if not device.startswith("cuda"):
        return None
    import torch

    return torch.cuda


def profile_adapter(
    adapter: Adapter,
    *,
    batch_sizes: Sequence[int] = (1, 2, 4, 8),
    iterations: int = 30,
    warmup: int = 3,
    inputs: ModelInputs | None = None,
) -> MeasuredProfile:
    """Time `iterations` forward passes per batch size after `warmup` untimed ones."""
    properties = adapter.properties
    sample = inputs or blank_inputs(adapter.binding)
    cuda = _cuda(properties.device)
    on_cuda = cuda is not None
    points = []
    for size in sorted(set(batch_sizes)):
        batch = [sample] * size
        for _ in range(warmup):
            adapter.infer(batch)
        if on_cuda:
            cuda.synchronize()
            cuda.reset_peak_memory_stats()
        timings = []
        for _ in range(iterations):
            start = time.perf_counter_ns()
            adapter.infer(batch)
            if on_cuda:
                cuda.synchronize()
            timings.append((time.perf_counter_ns() - start) // 1000)
        if on_cuda:
            memory = int(cuda.max_memory_allocated() // (1 << 20))
        else:
            memory = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)
        values = np.array(timings, dtype=np.int64)
        points.append(
            MeasuredPoint(
                batch_size=size,
                p50_us=int(np.percentile(values, 50)),
                p99_us=max(int(np.percentile(values, 99)), int(np.percentile(values, 50))),
                max_us=int(values.max()),
                memory_mib=memory,
                samples=len(timings),
            )
        )
    return MeasuredProfile(
        adapter=properties.name,
        device=properties.device,
        precision=properties.precision,
        points=tuple(points),
        memory_source="cuda_peak_allocated" if on_cuda else "process_peak_rss",
    )
