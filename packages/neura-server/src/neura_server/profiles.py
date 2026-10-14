"""Inference latency profiles indexed by batch size (design §9.2, §13).

A profile starts from measured (or configured) points and adapts upward from observed latencies, so
predictions become more conservative when reality is slower than the profile, never optimistic.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class ProfilePoint:
    batch_size: int
    p50_us: int
    p99_us: int
    memory_mib: int = 0


class LatencyProfile:
    def __init__(self, points: list[ProfilePoint], *, window: int = 256) -> None:
        if not points:
            raise ValueError("a profile needs at least one point")
        ordered = sorted(points, key=lambda p: p.batch_size)
        sizes = [p.batch_size for p in ordered]
        if sizes[0] < 1 or len(set(sizes)) != len(sizes):
            raise ValueError("batch sizes must be unique and >= 1")
        for point in ordered:
            if point.p50_us <= 0 or point.p99_us < point.p50_us:
                raise ValueError("latencies must be positive with p99 >= p50")
        self._points = ordered
        self._observed: dict[int, deque[int]] = {}
        self._window = window

    @property
    def max_profiled_batch(self) -> int:
        return self._points[-1].batch_size

    def _interpolate(self, batch_size: int, attr: str) -> float:
        xs = np.array([p.batch_size for p in self._points], dtype=np.float64)
        ys = np.array([getattr(p, attr) for p in self._points], dtype=np.float64)
        if len(xs) == 1:
            return float(ys[0] * batch_size / xs[0]) if batch_size > xs[0] else float(ys[0])
        if batch_size > xs[-1]:
            slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
            return float(ys[-1] + max(slope, 0.0) * (batch_size - xs[-1]))
        return float(np.interp(batch_size, xs, ys))

    def p99_us(self, batch_size: int) -> int:
        prior = self._interpolate(batch_size, "p99_us")
        observed = self._observed.get(batch_size)
        if observed and len(observed) >= 8:
            measured = float(np.percentile(np.fromiter(observed, dtype=np.int64), 99))
            return int(max(prior, measured))
        if observed:
            return int(max(prior, max(observed)))
        return int(prior)

    def p50_us(self, batch_size: int) -> int:
        return int(self._interpolate(batch_size, "p50_us"))

    def memory_mib(self, batch_size: int) -> int:
        return int(self._interpolate(batch_size, "memory_mib"))

    def observe(self, batch_size: int, latency_us: int) -> None:
        self._observed.setdefault(batch_size, deque(maxlen=self._window)).append(latency_us)
