"""Inference latency profiles indexed by batch size (design §9.2, §13).

A profile starts from measured (or configured) points and adapts upward from observed latencies, so
predictions become more conservative when reality is slower than the profile, never optimistic.

Two rules keep that adaptation from turning one bad moment into an outage (found by chaos testing:
a gateway host frozen for 3 s while a batch was in flight made every later admission fail, and
with no sessions admitted no new batch of that size ever ran to correct it):

- A single outlier never raises a prediction on its own. The observed tail is the latency that at
  most 1% of recent samples exceed, and never fewer than one: an isolated stall (a paused host, a
  garbage-collection pause) is ignored, a repeated slowdown is not.
- Observations expire after ``horizon_us``, so an inflated prediction recovers even when nothing
  is being served. If the slowdown is real, the next samples raise it again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

DEFAULT_HORIZON_US = 120_000_000


@dataclass(frozen=True, slots=True)
class ProfilePoint:
    batch_size: int
    p50_us: int
    p99_us: int
    memory_mib: int = 0


def _monotone(points: list[ProfilePoint]) -> list[ProfilePoint]:
    """A larger batch is never predicted faster or smaller than a smaller one. Measured points
    can say otherwise only through sampling noise (a tail sample landing in one batch size's
    window), and the conservative reading is the higher one."""
    out: list[ProfilePoint] = []
    for point in points:
        if out:
            last = out[-1]
            point = ProfilePoint(
                point.batch_size,
                max(point.p50_us, last.p50_us),
                max(point.p99_us, last.p99_us),
                max(point.memory_mib, last.memory_mib),
            )
        out.append(point)
    return out


class LatencyProfile:
    def __init__(
        self,
        points: list[ProfilePoint],
        *,
        window: int = 256,
        horizon_us: int = DEFAULT_HORIZON_US,
    ) -> None:
        if not points:
            raise ValueError("a profile needs at least one point")
        ordered = sorted(points, key=lambda p: p.batch_size)
        sizes = [p.batch_size for p in ordered]
        if sizes[0] < 1 or len(set(sizes)) != len(sizes):
            raise ValueError("batch sizes must be unique and >= 1")
        for point in ordered:
            if point.p50_us <= 0 or point.p99_us < point.p50_us:
                raise ValueError("latencies must be positive with p99 >= p50")
        self._points = _monotone(ordered)
        # Per batch size: (observed at, latency), oldest first.
        self._observed: dict[int, deque[tuple[int, int]]] = {}
        self._window = window
        self._horizon_us = horizon_us

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
        if not observed or len(observed) < 2:
            return int(prior)
        values = sorted(latency for _, latency in observed)
        tail = values[-1 - max(1, len(values) // 100)]
        return int(max(prior, tail))

    def p50_us(self, batch_size: int) -> int:
        return int(self._interpolate(batch_size, "p50_us"))

    def memory_mib(self, batch_size: int) -> int:
        return int(self._interpolate(batch_size, "memory_mib"))

    def observe(self, batch_size: int, latency_us: int, now_us: int = 0) -> None:
        self.expire(now_us)
        samples = self._observed.setdefault(batch_size, deque(maxlen=self._window))
        samples.append((now_us, latency_us))

    def expire(self, now_us: int) -> None:
        """Forget observations older than the horizon (``now_us`` on the observer's clock)."""
        for samples in self._observed.values():
            while samples and now_us - samples[0][0] > self._horizon_us:
                samples.popleft()
